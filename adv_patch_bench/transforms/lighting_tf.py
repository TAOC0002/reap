"""Calculate parameters for lighting transform."""

from __future__ import annotations

import logging
import math
from typing import Callable

import kornia
import kornia.geometry.transform as kornia_tf
import numpy as np
import torch
from sklearn.cluster import KMeans
from torch import nn

import adv_patch_bench.utils.image as img_util
from adv_patch_bench.transforms.geometric_tf import get_transform_matrix
from adv_patch_bench.utils.types import BatchImageTensor, BatchMaskTensor

_EPS = 1e-6
logger = logging.getLogger(__name__)
_ColorTF = Callable[[BatchImageTensor], BatchImageTensor]


class RGBtoLab(nn.Module):
    """Convert RGB to Lab color space."""

    def __init__(self) -> None:
        """Initialize RGBtoLab."""
        super().__init__()
        rgb_to_lms = torch.tensor(
            [
                [0.3811, 0.5783, 0.0402],
                [0.1967, 0.7244, 0.0782],
                [0.0241, 0.1288, 0.8444],
            ]
        )
        scale = torch.tensor(
            [
                [1 / math.sqrt(3), 0, 0],
                [0, 1 / math.sqrt(6), 0],
                [0, 0, 1 / math.sqrt(2)],
            ]
        )
        loglms_to_lab = scale @ torch.tensor(
            [[1.0, 1.0, 1.0], [1.0, 1.0, -2.0], [1.0, -1.0, 0.0]]
        )
        self.register_buffer("rgb_to_lms", rgb_to_lms)
        self.register_buffer("loglms_to_lab", loglms_to_lab)

    def forward(self, inputs: BatchImageTensor) -> BatchMaskTensor:
        """Forward pass."""
        # Clamp to avoid log(0)
        inputs = inputs.clamp(_EPS, 1 - _EPS)
        # Convert to LMS space and then Lab space
        lms = torch.einsum("bchw,cd->bdhw", inputs, self.rgb_to_lms)
        lab = torch.einsum("bchw,cd->bdhw", lms.log(), self.loglms_to_lab)
        return lab


class LabtoRGB(nn.Module):
    """Convert Lab to RGB color space."""

    def __init__(self) -> None:
        """Initialize LabtoRGB."""
        super().__init__()
        scale = torch.tensor(
            [
                [1 / math.sqrt(3), 0, 0],
                [0, 1 / math.sqrt(6), 0],
                [0, 0, 1 / math.sqrt(2)],
            ]
        )
        lab_to_loglms = (
            torch.tensor([[1.0, 1.0, 1.0], [1.0, 1.0, -1.0], [1.0, -2.0, 0.0]])
            @ scale
        )
        lms_to_rgb = torch.tensor(
            [
                [4.4679, -3.5873, 0.1193],
                [-1.2186, 2.3809, -0.1624],
                [0.0497, -0.2439, 1.2045],
            ]
        )
        self.register_buffer("lab_to_loglms", lab_to_loglms)
        self.register_buffer("lms_to_rgb", lms_to_rgb)

    def forward(self, inputs: BatchImageTensor) -> BatchMaskTensor:
        """Forward pass."""
        # Convert to LMS space and then Lab space
        loglms = torch.einsum("bchw,cd->bdhw", inputs, self.lab_to_loglms)
        rgb = torch.einsum("bchw,cd->bdhw", loglms.exp(), self.lms_to_rgb)
        return rgb


class RelightTransform(nn.Module):
    """Lighting transform for image."""

    def __init__(self, method: str = "color_transfer") -> None:
        """Initialize RelightTransform.

        Args:
            method: Method for lighting transform. Defaults to "color_transfer".
        """
        super().__init__()
        self._method: str = method
        self._channels = None
        self._color_tfs = None
        # Trying to use color transfer method for L channel only still affects
        # the color significantly. Using kornia's RGB<>Lab conversion helps a
        # little but still looks worse than polynomial method.

        if len(method.split("-")) == 1:
            channels = None
        else:
            channels = method.split("-")[1]

        color_space = method.split("_")[-1].split("-")[0]
        if color_space == "hsv":
            self._color_tfs = [
                kornia.color.RgbToHsv(),
                kornia.color.HsvToRgb(),
            ]
            if channels == "sv":
                self._channels = [1, 2]
        elif color_space == "lab":
            if method == "color_transfer_lab":
                self._color_tfs = [RGBtoLab(), LabtoRGB()]
            else:
                self._color_tfs = [
                    kornia.color.RgbToLab(),
                    kornia.color.LabToRgb(),
                ]
            if channels == "l":
                self._channels = [0]

        if self._color_tfs is None and "color_transfer" in method:
            raise ValueError(
                "Non-RGB color space transform is required for Color Transfer!"
            )

    def forward(
        self,
        inputs: BatchImageTensor,
        relight_coeffs: torch.Tensor | None = None,
    ) -> BatchImageTensor:
        """Forward pass.

        Args:
            inputs: Batched input images in RGB format.
                Shape: [batch_size, num_channels, H, W].
            relight_coeffs: Lighting coefficients for each image in the batch.

        Returns:
            Relighted images.
        """
        if "color_transfer" in self._method:
            func = _color_transfer
        elif "percentile" in self._method or "polynomial" in self._method:
            func = _polynomial_match
        elif self._method is None or self._method == "none":
            func = _no_relight
        else:
            raise NotImplementedError("Invalid lighting transform method!")
        return func(
            inputs,
            relight_coeffs,
            channels=self._channels,
            color_space_transforms=self._color_tfs,
        )


def _no_relight(
    inputs: BatchImageTensor,
    ct_coeffs: torch.Tensor,
    channels: list[int] | None = None,
    color_space_transforms: tuple[_ColorTF, _ColorTF] | None = None,
) -> BatchImageTensor:
    _ = ct_coeffs, channels, color_space_transforms  # Unused
    return inputs.clone()


def _color_transfer(
    inputs: BatchImageTensor,
    ct_coeffs: torch.Tensor,
    channels: list[int] | None = None,
    color_space_transforms: tuple[_ColorTF, _ColorTF] | None = None,
) -> BatchImageTensor:
    """Relight with Color Transfer method from Reinhard, et al. [2021].

    Reference: https://ieeexplore.ieee.org/document/946629.

    Args:
        inputs: Input images. Expect shape: [batch_size, num_channels, H, W].
        pixel_stats: Pixel statistics. Expect shape: [batch_size, num_channels].
        rgb_to_lab: Function to convert RGB to Lab color space.
        lab_to_rgb: Function to convert Lab to RGB color space.

    Returns:
        Relighted images.
    """
    # Convert to LMS space and then Lab space
    lab = color_space_transforms[0](inputs)

    # Compute mean and standard deviation of L, a, b channels and normalize
    ct_coeffs = ct_coeffs.view(-1, 3, 2, 1, 1)
    if channels is not None:
        lab_out = lab.clone()
        for channel in channels:
            lab_out[:, channel] = (
                lab[:, channel] * ct_coeffs[:, channel, 0]
                + ct_coeffs[:, channel, 1]
            )
    else:
        lab_out = lab * ct_coeffs[:, :, 0] + ct_coeffs[:, :, 1]

    # Convert back to LMS space and then RGB space
    rgb_out = color_space_transforms[1](lab_out)
    oob_pixels = (rgb_out < 0) | (rgb_out > 1)
    if oob_pixels.any():
        num_oob_pixels = oob_pixels.sum().item()
        logger.debug(
            "Found %d (out of %d) invalid RGB values (min: %.4f, max: %.4f) in "
            "Color Transfer! Clipping to [0, 1].",
            num_oob_pixels,
            inputs.numel(),
            rgb_out.min().item(),
            rgb_out.max().item(),
        )
        rgb_out = rgb_out.clamp(0, 1)
    return rgb_out


def _polynomial_match(
    inputs: BatchImageTensor,
    poly_coeffs: torch.Tensor,
    channels: list[int] | None = None,
    color_space_transforms: tuple[_ColorTF, _ColorTF] | None = None,
) -> BatchImageTensor:
    """Relight transform with polynomial function.

    Args:
        inputs: Input images. Expect shape: [batch_size, num_channels, H, W].
        poly_coeffs: Polynomial coefficients (highest degree first). Expect
            shape: [batch_size, num_channels, num_degree].

    Raises:
        ValueError: Invalid shape of poly_coeffs.
    """
    if poly_coeffs.ndim != 3:
        raise ValueError(
            "Expect poly_coeffs to have 3 dimensions [batch_size, "
            f"num_channels, num_degree], but got {poly_coeffs.ndim}!"
        )
    if len(poly_coeffs) != len(inputs) and len(poly_coeffs) != 1:
        raise ValueError(
            "poly_coeffs should have batch size of 1 or the same as inputs, "
            f"but got {len(poly_coeffs)}!"
        )
    if channels is None and poly_coeffs.shape[-2] not in (1, 3):
        raise ValueError(
            "poly_coeffs must have channel dimension of 1 or 3, but got "
            f"{poly_coeffs.shape[1]}!"
        )
    deg = poly_coeffs.shape[-1]
    device = inputs.device
    degrees = torch.arange(deg - 1, -1, -1, device=device).view(1, 1, deg, 1, 1)
    if color_space_transforms is not None:
        inputs = color_space_transforms[0](inputs)
    outputs = inputs[:, :, None].pow(degrees)

    if channels is None:
        outputs = torch.einsum("bcdhw, bcd -> bchw", outputs, poly_coeffs)
    else:
        new_outputs = []
        for channel in range(3):
            if channel in channels:
                tmp = (
                    outputs[:, channel]
                    * poly_coeffs[:, channels.index(channel), :, None, None]
                ).sum(1)
            else:
                tmp = inputs[:, channel]
            new_outputs.append(tmp)
        outputs = torch.stack(new_outputs, dim=1)

    if color_space_transforms is not None:
        outputs = color_space_transforms[1](outputs)
    outputs.clamp_(0, 1)
    return outputs


def _run_kmean_single(
    img: np.ndarray,
    k: int,
    keep_channel: bool = True,
    n_init: int = 10,
    max_iter: int = 300,
):
    # NOTE: Should we cluster by 1D data instead?
    kmean = KMeans(
        n_clusters=k, init="k-means++", n_init=n_init, max_iter=max_iter
    )
    img = img.reshape(-1, 1) if not keep_channel else img
    labels = kmean.fit_predict(img)
    return kmean.inertia_, kmean.cluster_centers_, labels


def _run_kmean(img: np.ndarray, keep_channel: bool = True):
    loss_2, centers_2, labels_2 = _run_kmean_single(
        img, 2, keep_channel=keep_channel
    )
    loss_3, centers_3, labels_3 = _run_kmean_single(
        img, 3, keep_channel=keep_channel
    )
    n = img.shape[-1]
    is_2 = _best_k(loss_2, loss_3, n)
    print("2" if is_2 else "3")
    centers = centers_2 if is_2 else centers_3
    labels = labels_2 if is_2 else labels_3
    return centers, labels


def _best_k(loss_2: float, loss_3: float, n: int):
    # k selection is based on the score proposed by
    # https://www.ee.columbia.edu/~dpwe/papers/PhamDN05-kmeans.pdf
    alpha_2 = 1 - 3 / (4 * n)
    alpha_3 = alpha_2 + (1 - alpha_2) / 6
    score_2 = loss_2 / alpha_2
    score_3 = loss_3 / (alpha_3 * loss_2)
    return score_2 < score_3


def _find_canonical_kmean(img: np.ndarray):
    centers, labels = _run_kmean(img, keep_channel=True)

    white_idx = centers.sum(-1).argmin()
    black_idx = centers.sum(-1).argmax()
    white = centers[white_idx]
    black = centers[black_idx]
    beta = black
    alpha = white - beta

    canonical = centers[labels]
    return canonical, alpha, beta


def _fit_polynomial(
    real_pixels_by_channel: list[torch.Tensor],
    syn_obj: torch.Tensor | None = None,
    warped_obj_mask: torch.Tensor | None = None,
    transform_mat: torch.Tensor | None = None,
    interp: str = "bilinear",
    polynomial_degree: int = 1,
    percentile: float = 0.0,
    mode: str = "",
) -> torch.Tensor:
    if not isinstance(syn_obj, torch.Tensor):
        raise ValueError("syn_obj must be provided as torch.Tensor.")

    syn_obj: BatchImageTensor = img_util.coerce_rank(syn_obj, 4)
    syn_obj = kornia_tf.warp_perspective(
        syn_obj,
        transform_mat,
        warped_obj_mask.shape[-2:],
        mode=interp,
        padding_mode="zeros",
    )
    if "hsv" in mode:
        syn_obj = kornia.color.rgb_to_hsv(syn_obj)
    elif "lab" in mode:
        syn_obj = kornia.color.rgb_to_lab(syn_obj)

    real_pixels_by_channel = torch.stack(real_pixels_by_channel, dim=0)
    channels = [0]
    if mode == "max":
        syn_obj = syn_obj.max(1, keepdim=True)[0]
        real_pixels_by_channel = real_pixels_by_channel.max(0, keepdim=True)[0]
    elif mode == "mean":
        syn_obj = syn_obj.mean(1, keepdim=True)
        real_pixels_by_channel = real_pixels_by_channel.mean(0, keepdim=True)
    elif "-sv" in mode:
        channels = [1, 2]
    elif "-l" in mode:
        channels = [0]
    else:
        channels = [0, 1, 2]

    coeffs = []
    for channel in channels:
        syn_pixels = torch.masked_select(syn_obj[:, channel], warped_obj_mask)
        real_pixels = real_pixels_by_channel[channel]

        # Drop some high values to reduce outliers
        num_kept = round((1 - percentile) * len(real_pixels))
        diff = (syn_pixels - real_pixels).abs()
        indices = torch.topk(diff, num_kept, largest=False).indices
        syn_pixels, real_pixels = syn_pixels[indices], real_pixels[indices]

        if syn_pixels.sum() == 0:
            poly = torch.zeros(polynomial_degree + 1)
            poly[-1] = real_pixels.mean()
        else:
            # Fit a polynomial to each channel independently
            poly = np.polyfit(
                syn_pixels.numpy(), real_pixels.numpy(), polynomial_degree
            )
            poly = torch.from_numpy(poly).float()
        coeffs.append(poly)

    return torch.stack(coeffs, dim=0)


def _get_color_transfer_params(
    real_pixels_by_channel: list[torch.Tensor],
    syn_obj: torch.Tensor | None = None,
    obj_mask: torch.Tensor | None = None,
    mode: str = "",
) -> torch.Tensor:
    if "hsv" in mode:
        syn_obj = kornia.color.rgb_to_hsv(syn_obj)
    elif "lab" in mode:
        syn_obj = RGBtoLab()(syn_obj)
    coeffs = torch.zeros(3, 2)
    for channel in range(3):
        syn_pixels = torch.masked_select(syn_obj[:, channel], obj_mask)
        real_pixels = real_pixels_by_channel[channel]
        # (x - syn_mean) * real_std / syn_std + real_mean
        coeffs[channel, 0] = real_pixels.std() / syn_pixels.std().clamp_min(
            _EPS
        )
        coeffs[channel, 1] = (
            real_pixels.mean() - syn_pixels.mean() * coeffs[channel, 0]
        )
    assert not coeffs.isnan().any(), "NaN in Color Transfer coeffs!"
    return coeffs


def _simple_percentile(
    real_pixels: list[torch.Tensor], mode: str = "", percentile: int = 10
) -> torch.Tensor:
    """Compute relighting transform by matching histogram percentiles.

    Args:
        real_pixels: 1D tensor of pixel values from real images.
        mode: Specific color space and channels to use.
        percentile: Percentile of pixels considered as min and max of scaling
            range. Only used when method is "percentile". Defaults to 10.0.
    """
    assert 0 <= percentile <= 1, percentile
    percentile = round(min(percentile, 1 - percentile) * 100)

    if "-sv" in mode:
        channels = [1, 2]
    elif "-l" in mode:
        channels = [0]
    else:
        channels = [0]
        real_pixels = torch.stack(real_pixels, dim=0)
        real_pixels = real_pixels.view(1, -1)

    coeffs = torch.zeros(len(channels), 2)
    for i, channel in enumerate(channels):
        real_pixels = real_pixels[channel].reshape(-1)
        min_ = np.nanpercentile(real_pixels, percentile)
        max_ = np.nanpercentile(real_pixels, 100 - percentile)
        coeffs[i] = torch.tensor([max_ - min_, min_], dtype=torch.float32)

    return coeffs


def compute_relight_params(
    img: torch.Tensor,
    method: str | None = "percentile",
    obj_mask: torch.Tensor | None = None,
    transform_mat: torch.Tensor | None = None,
    src_points: np.ndarray | None = None,
    tgt_points: np.ndarray | None = None,
    transform_mode: str = "perspective",
    interp: str = "bilinear",
    **relight_kwargs,
) -> torch.Tensor:
    """Compute params of relighting transform.

    Args:
        img: Image as Numpy array.
        method: Method to use for computing the relighting params. Defaults to
            "percentile".

    Returns:
        Relighting transform params, alpha and beta.
    """
    if img.size == 0 or method is None or method == "none":
        return 1.0, 0.0

    img: BatchImageTensor = img_util.coerce_rank(img, 4)
    obj_mask: BatchMaskTensor = img_util.coerce_rank(obj_mask, 4)

    if "percentile" in method or "polynomial" in method:
        if transform_mat is None:
            transform_mat = get_transform_matrix(
                src=src_points, tgt=tgt_points, transform_mode=transform_mode
            )
        obj_mask = kornia_tf.warp_perspective(
            obj_mask,
            transform_mat,
            img.shape[-2:],
            mode="nearest",
            padding_mode="zeros",
        )
    elif "color_transfer" in method:
        if transform_mat is None:
            # Swap source and target points
            transform_mat = get_transform_matrix(
                src=tgt_points, tgt=src_points, transform_mode=transform_mode
            )
        img = kornia_tf.warp_perspective(
            img,
            transform_mat,
            obj_mask.shape[-2:],
            mode=interp,
            padding_mode="zeros",
        )

    mode = method.split("_")[-1]
    if "hsv" in mode:
        img = kornia.color.rgb_to_hsv(img)
    elif "lab" in mode and "color_transfer" in method:
        img = RGBtoLab()(img)
    elif "lab" in mode:
        img = kornia.color.rgb_to_lab(img)

    obj_mask = obj_mask[:, 0] == 1
    real_pixels = []
    for channel in range(3):
        real_pixels.append(torch.masked_select(img[:, channel], obj_mask))

    if "percentile" in method:
        coeffs = _simple_percentile(real_pixels, mode=mode, **relight_kwargs)
    elif "polynomial" in method:
        coeffs = _fit_polynomial(
            real_pixels,
            warped_obj_mask=obj_mask,
            transform_mat=transform_mat,
            mode=mode,
            interp=interp,
            **relight_kwargs,
        )
    elif "color_transfer" in method:
        coeffs = _get_color_transfer_params(
            real_pixels, obj_mask=obj_mask, mode=mode, **relight_kwargs
        )
    else:
        raise NotImplementedError(f"Method {method} is not implemented!")

    return coeffs
