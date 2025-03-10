"""DPatch attack for Detectron2 models."""

from __future__ import annotations

import copy
from typing import Any

from torch import nn

from adv_patch_bench.attacks.dpatch import dpatch_yolo
from adv_patch_bench.attacks.rp2 import rp2_yolo


class DPatchYolofAttack(dpatch_yolo.DPatchYoloAttack):
    """DPatch Attack for YOLOF models."""

    def __init__(
        self, attack_config: dict[str, Any], core_model: nn.Module, **kwargs
    ) -> None:
        """Initialize RP2YoloAttack.

        Args:
            attack_config: Dictionary of attack params.
            core_model: Traget model to attack.
        """
        # We call RP2BaseAttack.__init__ instead of DPatchYoloAttack.__init__
        # to avoid setting incompatible model-specific parameters.
        super(rp2_yolo.RP2YoloAttack, self).__init__(
            attack_config, core_model, **kwargs
        )
        if hasattr(core_model, "module"):
            core_model = core_model.module
        self._nms_thres_orig = copy.deepcopy(core_model.test_nms_thresh)
        self._conf_thres_orig = copy.deepcopy(core_model.test_score_thresh)
        # loss_evaluators[0] is YOLOHead
        self._iou_thres_orig = copy.deepcopy(core_model.pos_ignore_thresh)
        if self._nms_thres is None:
            self._nms_thres = self._nms_thres_orig
        if self._min_conf is None:
            self._min_conf = self._conf_thres_orig
        if self._iou_thres is None:
            self._iou_thres = self._iou_thres_orig

    def _on_enter_attack(self, **kwargs) -> None:
        self._is_training = self._core_model.training
        self._core_model.eval()
        if hasattr(self._core_model, "module"):
            core_model = self._core_model.module
        else:
            core_model = self._core_model
        core_model.attack_mode = True
        core_model.test_nms_thresh = self._nms_thres
        core_model.test_score_thresh = self._min_conf
        core_model.pos_ignore_thresh = self._iou_thres

    def _on_exit_attack(self, **kwargs) -> None:
        self._core_model.train(self._is_training)
        if hasattr(self._core_model, "module"):
            core_model = self._core_model.module
        else:
            core_model = self._core_model
        core_model.attack_mode = False
        core_model.test_nms_thresh = self._nms_thres_orig
        core_model.test_score_thresh = self._conf_thres_orig
        core_model.pos_ignore_thresh = self._iou_thres_orig
