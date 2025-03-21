import argparse
import os
import random
import sys
import shutil
from importlib.machinery import SourceFileLoader

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, _BASE_DIR)

for p in sys.path:
    print(p)

import matplotlib.pyplot as plt
import cv2
import numpy as np
import torch
from tqdm import tqdm
import wandb

from datasets.gradslam_datasets import (
    load_dataset_config,
    ICLDataset,
    ReplicaDataset,
    ReplicaV2Dataset,
    AzureKinectDataset,
    ScannetDataset,
    Ai2thorDataset,
    Record3DDataset,
    RealsenseDataset,
    TUMDataset,
    ScannetPPDataset,
)
from utils.common_utils import seed_everything
from utils.eval_helpers import eval, eval_nvs


def get_dataset(config_dict, basedir, sequence, **kwargs):
    if config_dict["dataset_name"].lower() in ["icl"]:
        return ICLDataset(config_dict, basedir, sequence, **kwargs)
    elif config_dict["dataset_name"].lower() in ["replica"]:
        return ReplicaDataset(config_dict, basedir, sequence, **kwargs)
    elif config_dict["dataset_name"].lower() in ["replicav2"]:
        return ReplicaV2Dataset(config_dict, basedir, sequence, **kwargs)
    elif config_dict["dataset_name"].lower() in ["azure", "azurekinect"]:
        return AzureKinectDataset(config_dict, basedir, sequence, **kwargs)
    elif config_dict["dataset_name"].lower() in ["scannet"]:
        return ScannetDataset(config_dict, basedir, sequence, **kwargs)
    elif config_dict["dataset_name"].lower() in ["ai2thor"]:
        return Ai2thorDataset(config_dict, basedir, sequence, **kwargs)
    elif config_dict["dataset_name"].lower() in ["record3d"]:
        return Record3DDataset(config_dict, basedir, sequence, **kwargs)
    elif config_dict["dataset_name"].lower() in ["realsense"]:
        return RealsenseDataset(config_dict, basedir, sequence, **kwargs)
    elif config_dict["dataset_name"].lower() in ["tum"]:
        return TUMDataset(config_dict, basedir, sequence, **kwargs)
    elif config_dict["dataset_name"].lower() in ["scannetpp"]:
        return ScannetPPDataset(basedir, sequence, **kwargs)
    else:
        raise ValueError(f"Unknown dataset name {config_dict['dataset_name']}")


def load_scene_data(scene_path, params_opt_exclude, device="cuda"):
    params = dict(np.load(scene_path, allow_pickle=True))
    for k in params.keys():
        if k not in params_opt_exclude:
            params[k] = torch.tensor(params[k]).to(device).float().requires_grad_(True)
        else:
            params[k] = torch.tensor(params[k]).to(device)
    return params


def filter_semantic_params(params, to_remove_ids):
    if 'semantic_ids' in params:
        semantic_ids = params['semantic_ids']
        to_remove_ids = torch.tensor(to_remove_ids, device=semantic_ids.device,
                                     dtype=semantic_ids.dtype).unsqueeze(0)
        to_remove_mask = (semantic_ids == to_remove_ids).any(dim=1)
        to_keep_mask = ~to_remove_mask
        keys = [k for k in params.keys() if k not in ['cam_unnorm_rots', 'cam_trans']]
        for k in keys:
            params[k] = params[k][to_keep_mask]
    return params


if __name__=="__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("experiment", type=str, help="Path to experiment file")

    args = parser.parse_args()

    experiment = SourceFileLoader(
        os.path.basename(args.experiment), args.experiment
    ).load_module()
    
    config = experiment.config
    params_opt_exclude = set()

    # Set Experiment Seed
    seed_everything(seed=experiment.config['seed'])
    device = torch.device(config["primary_device"])
    if experiment.config["primary_device"].startswith("cuda:"):
        device_id = int(experiment.config["primary_device"].split(':')[1])
        torch.cuda.set_device(device_id)

    # Create Results Directory and Copy Config
    results_dir = os.path.join(
        experiment.config["workdir"], experiment.config["run_name"]
    )
    if not experiment.config['load_checkpoint']:
        os.makedirs(results_dir, exist_ok=True)
        shutil.copy(args.experiment, os.path.join(results_dir, "config.py"))

    if "scene_path" not in experiment.config:
        results_dir = os.path.join(
            experiment.config["workdir"], experiment.config["run_name"]
        )
        scene_path = os.path.join(results_dir, "params.npz")
    else:
        scene_path = experiment.config["scene_path"]

    # Load Dataset
    print("Loading Dataset ...")
    dataset_config = config["data"]
    if "gradslam_data_cfg" not in dataset_config:
        gradslam_data_cfg = {}
        gradslam_data_cfg["dataset_name"] = dataset_config["dataset_name"]
    else:
        gradslam_data_cfg = load_dataset_config(dataset_config["gradslam_data_cfg"])
    if "ignore_bad" not in dataset_config:
        dataset_config["ignore_bad"] = False
    if "use_train_split" not in dataset_config:
        dataset_config["use_train_split"] = True
    if "load_semantics" in dataset_config:
        load_semantics = dataset_config["load_semantics"]
        num_semantic_classes = dataset_config["num_semantic_classes"]
        params_opt_exclude.add('semantic_ids')
    else:
        load_semantics = False
        num_semantic_classes = 0
    # Poses are relative to the first training frame
    dataset = get_dataset(
        config_dict=gradslam_data_cfg,
        basedir=dataset_config["basedir"],
        sequence=os.path.basename(dataset_config["sequence"]),
        start=dataset_config["start"],
        end=dataset_config["end"],
        stride=dataset_config["stride"],
        desired_height=dataset_config["desired_image_height"],
        desired_width=dataset_config["desired_image_width"],
        device=device,
        relative_pose=True,
        ignore_bad=dataset_config["ignore_bad"],
        use_train_split=dataset_config["use_train_split"],
        load_semantics=load_semantics,
        num_semantic_classes=num_semantic_classes,
    )
    num_frames = dataset_config["num_frames"]

    if num_frames == -1:
        num_frames = len(dataset)

    params = load_scene_data(scene_path, params_opt_exclude)
    
    # if load_semantics:
    #     params = filter_semantic_params(params, to_remove_ids=[14])

    if dataset_config['use_train_split']:
        eval_dir = os.path.join(results_dir, "eval_train")
        wandb_name = config['wandb']['name'] + "_Train_Split"
    else:
        eval_dir = os.path.join(results_dir, "eval_nvs")
        wandb_name = config['wandb']['name'] + "_NVS_Split"
    
    # Init WandB
    if config['use_wandb']:
        wandb_time_step = 0
        wandb_tracking_step = 0
        wandb_mapping_step = 0
        wandb_run = wandb.init(project=config['wandb']['project'],
                               entity=config['wandb']['entity'],
                               group=config['wandb']['group'],
                               name=wandb_name,
                               config=config)

    # Evaluate Final Parameters
    with torch.no_grad():
        if config['use_wandb']:
            if dataset_config['use_train_split']:
                eval(dataset, params, num_frames, eval_dir, sil_thres=config['mapping']['sil_thres'],
                    wandb_run=wandb_run, wandb_save_qual=config['wandb']['eval_save_qual'],
                    mapping_iters=config['mapping']['num_iters'], add_new_gaussians=config['mapping']['add_new_gaussians'],
                    load_semantics=load_semantics, eval_every=config['eval_every'], save_frames=True)
            else:
                eval_nvs(dataset, params, num_frames, eval_dir, sil_thres=config['mapping']['sil_thres'],
                    wandb_run=wandb_run, wandb_save_qual=config['wandb']['eval_save_qual'],
                    mapping_iters=config['mapping']['num_iters'], add_new_gaussians=config['mapping']['add_new_gaussians'],
                    load_semantics=load_semantics, eval_every=config['eval_every'], save_frames=True)
        else:
            if dataset_config['use_train_split']:
                eval(dataset, params, num_frames, eval_dir, sil_thres=config['mapping']['sil_thres'],
                    mapping_iters=config['mapping']['num_iters'], add_new_gaussians=config['mapping']['add_new_gaussians'],
                    load_semantics=load_semantics, eval_every=config['eval_every'], save_frames=True)
            else:
                eval_nvs(dataset, params, num_frames, eval_dir, sil_thres=config['mapping']['sil_thres'],
                    mapping_iters=config['mapping']['num_iters'], add_new_gaussians=config['mapping']['add_new_gaussians'],
                    load_semantics=load_semantics, eval_every=config['eval_every'], save_frames=True)
    
    # Close WandB
    if config['use_wandb']:
        wandb_run.finish()
