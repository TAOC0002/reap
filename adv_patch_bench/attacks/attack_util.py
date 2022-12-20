"""Simple utility functions for preparing attacks."""

from __future__ import annotations

import pickle
from typing import Any, Dict, Optional, Tuple

import detectron2
import torch
import torchvision

import adv_patch_bench.utils.image as img_util
from adv_patch_bench.attacks import base_attack, patch_mask_util
from adv_patch_bench.attacks.dpatch import dpatch_detectron
from adv_patch_bench.attacks.rp2 import rp2_detectron, rp2_yolo
from adv_patch_bench.utils.types import (
    BatchImageTensor,
    BatchMaskTensor,
    SizeMM,
    SizePx,
)
from hparams import DEFAULT_PATH_DEBUG_PATCH

_ATTACK_DICT = {
    "rp2-detectron": rp2_detectron.RP2AttackDetectron,
    "rp2-yolo": rp2_yolo.RP2AttackYOLO,
    "dpatch-detectron": dpatch_detectron.DPatchAttackDetectron,
}


def setup_attack(
    config_attack: Optional[Dict[Any, str]] = None,
    is_detectron: bool = True,
    model: Optional[torch.nn.Module] = None,
    input_size: Tuple[int, int] = (1536, 2048),
    verbose: bool = False,
) -> base_attack.DetectorAttackModule:
    """Set up attack object."""
    # TODO(feature): Add no_attack as an attack option.
    attack_name: str = config_attack["common"]["attack_name"]
    if is_detectron:
        attack_fn_name: str = f"{attack_name}-detectron"
    else:
        attack_fn_name: str = f"{attack_name}-yolo"
    attack_fn = _ATTACK_DICT[attack_fn_name]
    combined_config_attack: Dict[str, Any] = {
        **config_attack["common"],
        **config_attack[attack_name],
    }

    return attack_fn(
        combined_config_attack,
        model,
        input_size=input_size,
        verbose=verbose,
    )


def prep_adv_patch(
    attack_type: str = "none",
    adv_patch_path: str | None = None,
    patch_size_mm: tuple[int, float, float] | None = None,
    obj_size_px: SizePx | None = None,
    obj_size_mm: SizeMM | None = None,
) -> tuple[BatchImageTensor | None, BatchMaskTensor | None]:
    """Load and prepare adversarial patch along with its mask.

    Args:
        attack_type (str, optional): Type of attack to run. Options are "none",
            "debug", "random", "load". Defaults to "none".
        adv_patch_path (Optional[str], optional): Path to pickle file containing
            adversarial patch and its mask. Defaults to None.
        patch_size_mm: Tuple (num_patch, height, width). Height and width are
            in millimeters, and num_patch must be int.
        obj_size_px: Size of object to place patch on in pixels.
        obj_size_mm: Size of object to place patch on in millimeters.

    Returns:
        Tuple of adversarial patch and patch mask.
    """
    if attack_type == "none":
        return None, None

    adv_patch: BatchImageTensor | None = None
    patch_mask: BatchMaskTensor | None = None

    if attack_type == "load":
        if adv_patch_path is None:
            raise ValueError(
                'If attack_type is "load", adv_patch_path must be specified!'
            )
        with open(adv_patch_path, "rb") as file:
            adv_patch, patch_mask = pickle.load(file)
    else:
        if patch_size_mm is None or obj_size_px is None or obj_size_mm is None:
            raise ValueError(
                "patch_size_mm, obj_size_px, obj_size_mm must be specified when "
                'attack_type is not "none" or "load".'
            )

        # Generate new patch mask from given sizes
        patch_mask = patch_mask_util.gen_patch_mask(
            patch_size_mm,
            obj_size_px,
            obj_size_mm,
        )

        if attack_type == "debug":
            # Load 'arrow on checkboard' patch if specified (for debug)
            debug_patch_path: str = DEFAULT_PATH_DEBUG_PATCH
            loaded_image: torch.Tensor = torchvision.io.read_image(
                debug_patch_path
            )
            adv_patch = loaded_image.float()[:3, :, :] / 255
        elif attack_type == "random":
            # Patch with uniformly random pixels between [0, 1]
            adv_patch = torch.rand((3,) + obj_size_px)

    pad_size = (obj_size_px[1], obj_size_px[1])
    if adv_patch is not None:
        adv_patch = img_util.resize_and_pad(
            obj=adv_patch,
            resize_size=pad_size,
            pad_size=pad_size,
            keep_aspect_ratio=True,
        )
    patch_mask = img_util.resize_and_pad(
        obj=patch_mask,
        resize_size=pad_size,
        pad_size=pad_size,
        keep_aspect_ratio=True,
        is_binary=True,
    )
    img_util.coerce_rank(adv_patch, 4)
    img_util.coerce_rank(patch_mask, 4)
    return adv_patch, patch_mask


def prep_adv_patch_all_classes(
    dataset: str = "mtsd_no_color",
    attack_type: str = "none",
    adv_patch_paths: list[str] | None = None,
    patch_size_mm: tuple[int, float, float] | None = None,
    obj_width_px: int = 64,
) -> tuple[list[BatchImageTensor | None], list[BatchMaskTensor | None]]:

    metadata = detectron2.data.MetadataCatalog.get(dataset)
    obj_dim_dict = metadata.get("obj_dim_dict")
    size_mm_dict = obj_dim_dict.get("size_mm")
    hw_ratio_dict = obj_dim_dict.get("hw_ratio")
    adv_patches, patch_masks = [], []

    for i, hw_ratio in hw_ratio_dict.items():
        adv_patch_path = None
        if adv_patch_paths is not None:
            adv_patch_path = adv_patch_paths[i]
        if i == metadata.get("bg_class"):
            continue
        obj_size_px = (round(hw_ratio * obj_width_px), obj_width_px)
        adv_patch, patch_mask = prep_adv_patch(
            attack_type=attack_type,
            adv_patch_path=adv_patch_path,
            patch_size_mm=patch_size_mm,
            obj_size_px=obj_size_px,
            obj_size_mm=size_mm_dict[i],
        )
        adv_patches.append(adv_patch)
        patch_masks.append(patch_mask)

    return adv_patches, patch_masks
