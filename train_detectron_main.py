"""Train script for Detectron2.

Code is adapted from train_net.py.
"""

from __future__ import annotations

import logging
import os
import pickle
import sys
import warnings
from collections import OrderedDict
from datetime import timedelta
from typing import Any

import torch
import torchvision
from detectron2.checkpoint import DetectionCheckpointer, PeriodicCheckpointer
from detectron2.data import (
    build_detection_test_loader,
    build_detection_train_loader,
)
from detectron2.data.build import (
    RepeatFactorTrainingSampler,
    get_detection_dataset_dicts,
)
from detectron2.engine import default_writers, launch
from detectron2.evaluation import (
    COCOEvaluator,
    inference_on_dataset,
    print_csv_format,
)
from detectron2.utils import comm
from detectron2.utils.events import EventStorage
from yolof.checkpoint import YOLOFCheckpointer

import adv_patch_bench.dataloaders.detectron.util as data_util
import adv_patch_bench.utils.docker_bug_fixes  # pylint: disable=unused-import
from adv_patch_bench.attacks import attack_util
from adv_patch_bench.dataloaders.detectron import (
    mtsd_dataset_mapper,
    mtsd_yolo_dataset_mapper,
)
from adv_patch_bench.models.custom_build import build_model
from adv_patch_bench.models.optimizer import build_lr_scheduler, build_optimizer
from adv_patch_bench.transforms.render_image import RenderImage
from adv_patch_bench.transforms.render_object import RenderObject
from adv_patch_bench.utils.argparse import reap_args_parser, setup_detectron_cfg
from adv_patch_bench.utils.types import BatchImageTensor

_EPS = 1e-6
logger = logging.getLogger(__name__)
# This is to ignore a warning from detectron2/structures/keypoints.py:29
warnings.filterwarnings("ignore", category=UserWarning)


def _get_sampler(cfg):
    """Define a custom process to get training sampler.

    This error is caused by torch.trunc raising a segfault (floating point
    exception) on pytorch docker image. Calling repeat_factors.long() before
    passing it to torch.trunc fixes this.
    """
    if cfg.DATALOADER.SAMPLER_TRAIN != "RepeatFactorTrainingSampler":
        return None
    dataset = get_detection_dataset_dicts(
        cfg.DATASETS.TRAIN,
        filter_empty=cfg.DATALOADER.FILTER_EMPTY_ANNOTATIONS,
        min_keypoints=0,
        proposal_files=None,
    )
    repeat_factors = (
        RepeatFactorTrainingSampler.repeat_factors_from_category_frequency(
            dataset, cfg.DATALOADER.REPEAT_THRESHOLD
        )
    )
    # This line is the fix
    repeat_factors = repeat_factors.long()
    try:
        sampler = RepeatFactorTrainingSampler(repeat_factors)
    except RuntimeError:
        # Handle different CUDA/pytorch version
        sampler = RepeatFactorTrainingSampler(repeat_factors.float())
    return sampler


# Need cfg/config for launch. pylint: disable=redefined-outer-name
def _get_evaluator(cfg, dataset_name, output_folder=None):
    """Create evaluator."""
    if output_folder is None:
        output_folder = os.path.join(cfg.OUTPUT_DIR, "inference")
    return COCOEvaluator(dataset_name, output_dir=output_folder)


# Need cfg/config for launch. pylint: disable=redefined-outer-name
def evaluate(cfg, config, model):
    """Evaluate model (validate or test)."""
    _ = config  # Unused for now
    results = OrderedDict()
    for dataset_name in cfg.DATASETS.TEST:
        # pylint: disable=missing-kwoa,too-many-function-args
        data_loader = build_detection_test_loader(
            cfg,
            dataset_name,
            batch_size=int(cfg.SOLVER.IMS_PER_BATCH / comm.get_world_size()),
        )
        evaluator = _get_evaluator(
            cfg,
            dataset_name,
            os.path.join(cfg.OUTPUT_DIR, "inference", dataset_name),
        )
        results_i = inference_on_dataset(model, data_loader, evaluator)
        results[dataset_name] = results_i
        if comm.is_main_process():
            logger.info(
                "Evaluation results for %s in csv format:", dataset_name
            )
            print_csv_format(results_i)
    if len(results) == 1:
        results = list(results.values())[0]
    return results


# Need cfg/config for launch. pylint: disable=redefined-outer-name
def train(cfg, config, model, attack):
    """Main training loop."""
    config_base = config["base"]
    use_attack: bool = config_base["attack_type"] != "none"
    train_dataset = cfg.DATASETS.TRAIN[0]
    use_ddp = torch.distributed.is_initialized()

    rimg_kwargs: dict[str, Any] = {
        "img_mode": cfg.INPUT.FORMAT,
        "interp": config_base["interp"],
        "img_aug_prob_geo": config_base["img_aug_prob_geo"],
        "device": model.device,
        "obj_class": config_base["obj_class"],
        "mode": config_base["dataset"].split("-")[0],
    }
    robj_kwargs = {
        "dataset": config_base["dataset"],
        "obj_size_px": config_base["obj_size_px"],
        "interp": config_base["interp"],
        **{k: v for k, v in config_base.items() if "reap" in k},
    }
    # Get augmentation for mask only
    _, trn_aug_mask, trn_aug_color = RenderObject.get_augmentation(
        config["attack"]["common"], "nearest"
    )

    model.train()
    optimizer = build_optimizer(cfg, model)
    scheduler = build_lr_scheduler(cfg, optimizer)

    if cfg.MODEL.META_ARCHITECTURE == "YOLOF":
        checkpointer_fn = YOLOFCheckpointer
    else:
        checkpointer_fn = DetectionCheckpointer
    checkpointer = checkpointer_fn(
        model, cfg.OUTPUT_DIR, optimizer=optimizer, scheduler=scheduler
    )

    if config_base["resume"]:
        # If resume, load from last checkpoint
        weight_path = checkpointer.get_checkpoint_file()
        start_iter = checkpointer.resume_or_load(weight_path, resume=True).get(
            "iteration", -1
        )
        scheduler.last_epoch = start_iter
    else:
        # If not resume, load from specified weight and start from itearation 0
        checkpointer.resume_or_load(cfg.MODEL.WEIGHTS, resume=False)
        start_iter = -1
    start_iter += 1
    max_iter = cfg.SOLVER.MAX_ITER

    periodic_checkpointer = PeriodicCheckpointer(
        checkpointer, cfg.SOLVER.CHECKPOINT_PERIOD, max_iter=max_iter
    )

    writers = (
        default_writers(cfg.OUTPUT_DIR, max_iter)
        if comm.is_main_process()
        else []
    )

    # Create patch masks (and load adv_patches if attack-type is load)
    logger.info("Preparing adversarial patches and masks (if applicable)...")
    adv_patches, patch_masks = attack_util.prep_adv_patch_all_classes(
        dataset=train_dataset,
        attack_type=config_base["attack_type"],
        patch_size=config_base["patch_size"],
        obj_width_px=config_base["obj_size_px"][1],
    )
    for i, (adv_patch, patch_mask) in enumerate(zip(adv_patches, patch_masks)):
        if adv_patch is not None:
            adv_patches[i] = adv_patch.to(model.device)
        if patch_mask is not None:
            patch_masks[i] = patch_mask.to(model.device)

    # Initialize and load cached adv_patch_cache when resuming
    adv_patch_cache = {}
    cache_file_name = f"{cfg.OUTPUT_DIR}/trn_adv_patch_cache.pkl"
    if (
        start_iter > 10
        and config_base["attack_type"] == "per-sign"
        and os.path.isfile(cache_file_name)
    ):
        logger.info("Loading cached adv_patch from %s", cache_file_name)
        with open(cache_file_name, "rb") as file:
            try:
                adv_patch_cache = pickle.load(file)
            except pickle.UnpicklingError:
                logger.warning(
                    "Failed to load adv_patch_cache. Initializing "
                    "adv_patch_cache from scratch."
                )

    sampler = _get_sampler(cfg)
    if cfg.MODEL.META_ARCHITECTURE == "YOLOF":
        logger.info("Using YOLOF dataset mapper")
        dataset_mapper_fn = mtsd_yolo_dataset_mapper.MtsdYoloDatasetMapper
    else:
        dataset_mapper_fn = mtsd_dataset_mapper.MtsdDatasetMapper
    # pylint: disable=missing-kwoa,too-many-function-args
    data_loader = build_detection_train_loader(
        cfg,
        sampler=sampler,
        mapper=dataset_mapper_fn(
            cfg,
            config_base=config_base,
            is_train=True,
            img_size=config_base["img_size"],
        ),
    )
    logger.info("Starting training from iteration %d", start_iter)
    with EventStorage(start_iter) as storage:
        for data, iteration in zip(data_loader, range(start_iter, max_iter)):
            storage.iter = iteration
            batch_size = len(data)

            data_adv = data
            if config_base["use_mixed_batch"] and not use_ddp:
                # This only works for single GPU training. Trying to have an
                # uneven batch size for DDP will hang indefinitely. When DDP is
                # used, we run attack on the entire batch.
                assert len(data) > 1, "Mixed batch requires at least 2 samples!"
                data_adv = data[: batch_size // 2]
                data = data[batch_size // 2 :]

            if use_attack:
                # Create image wrapper that handles tranforms
                rimg: RenderImage = RenderImage(
                    samples=data_adv, robj_kwargs=robj_kwargs, **rimg_kwargs
                )
                if rimg.num_objs > 0:
                    # Collect patch mask for each class because relative patch
                    # size varies between classes
                    cur_patch_mask = [patch_masks[i] for i in rimg.obj_classes]
                    cur_patch_mask = torch.cat(cur_patch_mask, dim=0)
                    assert len(cur_patch_mask) == rimg.num_objs

                    # Apply geometric augmentation to adversarial patch mask
                    cur_patch_mask = trn_aug_mask(cur_patch_mask)

                    if config_base["attack_type"] == "per-sign":
                        # Load cached adversarial patches
                        init_adv_patch = [
                            adv_patch_cache.get(oid) for oid in rimg.obj_ids
                        ]
                        # Generate per-sign patch for adversarial training
                        cur_adv_patch: BatchImageTensor = attack(
                            rimg,
                            cur_patch_mask,
                            batch_mode=True,
                            init_adv_patch=init_adv_patch,
                        )
                        # Cache generated adversarial patches for next epoch
                        adv_patch_cpu = cur_adv_patch.cpu()
                        for patch, oid in zip(adv_patch_cpu, rimg.obj_ids):
                            adv_patch_cache[oid] = patch.cpu()
                    else:
                        cur_adv_patch = [
                            adv_patches[i] for i in rimg.obj_classes
                        ]
                        cur_adv_patch = torch.cat(cur_adv_patch, dim=0)
                    cur_adv_patch.clamp_(0 + _EPS, 1 - _EPS)

                    # Apply color augmentation to adversarial patch
                    cur_adv_patch = trn_aug_color(cur_adv_patch)

                    img_render, data_adv = rimg.apply_objects(
                        cur_adv_patch, cur_patch_mask
                    )

                    if config_base["debug"]:
                        logger.debug(
                            "Saving debug training batch %d...", iteration
                        )
                        torchvision.utils.save_image(
                            img_render, f"tmp_train_debug_{iteration:05d}.png"
                        )

                    img_render = rimg.post_process_image(img_render)
                    for i, dataset_dict in enumerate(data_adv):
                        dataset_dict["image"] = img_render[i]

            if config_base["use_mixed_batch"] and not use_ddp:
                data = [*data_adv, *data]
            elif config_base["use_mixed_batch"]:
                data = [*data_adv[:batch_size // 2], *data[batch_size // 2:]]
            else:
                # Normal training or adversarial training w/o use_mixed_batch
                data = data_adv

            loss_dict = model(data)
            losses = sum(loss_dict.values())
            assert torch.isfinite(
                losses
            ).all(), f"Loss diverges; Something went wrong\n{loss_dict}"

            loss_dict_reduced = {
                k: v.item() for k, v in comm.reduce_dict(loss_dict).items()
            }
            if comm.is_main_process():
                losses_reduced = sum(loss_dict_reduced.values())
                storage.put_scalars(
                    total_loss=losses_reduced, **loss_dict_reduced
                )

            optimizer.zero_grad()
            losses.backward()
            optimizer.step()
            storage.put_scalar(
                "lr", optimizer.param_groups[0]["lr"], smoothing_hint=False
            )
            scheduler.step()

            if (
                cfg.TEST.EVAL_PERIOD > 0
                and (iteration + 1) % cfg.TEST.EVAL_PERIOD == 0
                and iteration != max_iter - 1
            ):
                evaluate(cfg, config, model)
                # Compared to "train_net.py", the test results are not dumped
                # to EventStorage
                comm.synchronize()

            if iteration - start_iter > 5 and (
                (iteration + 1) % 20 == 0 or iteration == max_iter - 1
            ):
                for writer in writers:
                    writer.write()
            periodic_checkpointer.step(iteration)
            if (iteration + 1) % periodic_checkpointer.period == 0:
                # Manually checkpoint cached adv patch
                with open(cache_file_name, "wb") as file:
                    pickle.dump(adv_patch_cache, file)


# Need cfg/config for launch. pylint: disable=redefined-outer-name
def main(config):
    """Main function."""
    cfg = setup_detectron_cfg(config, is_train=True)

    # Set logging config
    logging.basicConfig(
        stream=sys.stdout,
        format="[%(asctime)s - %(name)s - %(levelname)s]: %(message)s",
        level=config["base"]["verbosity"],
    )
    logger.setLevel(config["base"]["verbosity"])
    logging.getLogger("detectron2").setLevel(config["base"]["verbosity"])
    logging.getLogger("fvcore").setLevel(config["base"]["verbosity"])
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    data_util.register_dataset(config["base"])

    logger.info("Building model...")
    model = build_model(cfg)
    logger.info("Model:\n%s", model)

    # Set up attack for adversarial training
    attack = attack_util.setup_attack(config=config, model=model)

    train(cfg, config, model, attack)
    logger.info("Start final testing...")
    return evaluate(cfg, config, model)


if __name__ == "__main__":
    config: dict[str, dict[str, Any]] = reap_args_parser(
        True, is_gen_patch=False, is_train=True
    )
    launch(
        main,
        config["base"]["num_gpus"],
        num_machines=config["base"]["num_machines"],
        machine_rank=config["base"]["machine_rank"],
        dist_url=config["base"]["dist_url"],
        args=(config,),
        timeout=timedelta(hours=2),  # Set custom timeout
    )
