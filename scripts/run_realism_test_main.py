"""Run realism test for REAP benchmark."""

from __future__ import annotations

import math
import os
import pathlib
import pickle
from typing import Any

import cv2 as cv
import kornia.geometry.transform as kornia_tf
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import skimage
import torch
import torchvision
import torchvision.transforms.functional as F
from kornia.geometry.transform import get_perspective_transform
from torchvision.utils import make_grid, save_image
from tqdm import tqdm

import adv_patch_bench.utils.image as img_util
from adv_patch_bench.transforms import lighting_tf, util
from adv_patch_bench.utils.realism import compute_relight_params
from hparams import DATASET_METADATA, TS_COLOR_DICT

# list of point colors for visualizing image points
POINT_COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
IMG_WIDTH = 2048


def _save_images(
    image,
    src,
    tgt,
    patch_src,
    patch_src_transformed,
    patch_tgt,
    img_width,
    img_height,
    is_clean,
):
    if patch_tgt is None:
        patch_tgt = [None for _ in patch_src]

    for i, (src_i, tgt_i) in enumerate(zip(src, tgt)):
        cv.circle(image, (int(src_i[0]), int(src_i[1])), 5, POINT_COLORS[i], -1)
        cv.circle(image, (int(tgt_i[0]), int(tgt_i[1])), 5, POINT_COLORS[i], -1)

    for i, (src_i, srct_i, tgt_i) in enumerate(
        zip(patch_src, patch_src_transformed, patch_tgt)
    ):
        cv.circle(image, (int(src_i[0]), int(src_i[1])), 5, POINT_COLORS[i], -1)
        cv.circle(
            image, (int(srct_i[0]), int(srct_i[1])), 5, POINT_COLORS[i], -1
        )
        if not is_clean:
            cv.circle(
                image, (int(tgt_i[0]), int(tgt_i[1])), 5, POINT_COLORS[i], -1
            )
    # resize image and scale down by 8
    image_resized = cv.resize(image, (img_width // 8, img_height // 8))
    image_resized = torch.from_numpy(image_resized).permute(2, 0, 1)
    return image_resized.float() / 255


def main(
    geo_method: str,
    relight_method: str,
    relight_params: dict[str, Any] | None = None,
    use_jpeg: bool = True,
):
    """Main function for running realism test."""
    # file directory where images are stored
    # file_dir = "~/data/reap-benchmark/reap_realism_test/images_jpg/"
    file_dir = f"{DATA_DIR}/images/"
    file_dir = os.path.expanduser(file_dir)

    # path to directory where patch files are stored
    patch_path = pathlib.Path(
        f"{DATA_DIR}/synthetic-load-64-15-1-0.4-0-0-pd64-bg50-augimg1-rp2_1e-05_0_1_1000_adam_0.01_False"
    )
    patch_path = patch_path.expanduser()

    # read in annotation data from csv file
    annotation_df = pd.read_csv("realism_test_anno.csv")

    obj_class_to_shape = {
        "circle": "circle",
        "triangle": "triangle",
        "up-triangle": "triangle_inverted",
        "rect-s": "rect",
        "rect-m": "rect",
        "rect-l": "rect",
        "diamond-s": "diamond",
        "diamond-l": "diamond",
        "pentagon": "pentagon",
        "octagon": "octagon",
        "square": "square",
    }

    # list of traffic sign classes
    traffic_sign_classes = list(TS_COLOR_DICT.keys())
    # remove 'other' class from list
    traffic_sign_classes.remove("other")

    # lists to store geometric and lighting errors for each image
    geometric_errors = []
    lighting_errors = []
    all_crops = []

    relight_transform = lighting_tf.RelightTransform(method=relight_method)

    for index, row in tqdm(annotation_df.iterrows()):
        is_clean = index % 2 == 0
        if is_clean:
            relight_coeffs = None
        else:
            assert (
                relight_coeffs is not None
            ), "relight_coeffs must be specified for adversarial images"

        # obj_class = traffic_sign_classes[(index // 4) % len(traffic_sign_classes)]
        obj_class = traffic_sign_classes[
            (index // 2) % len(traffic_sign_classes)
        ]

        # get file path for image
        filename = row["file_name"]
        if use_jpeg:
            filename = filename.replace(".png", ".jpg")

        filepath = os.path.join(file_dir, filename)
        # check if file exists
        if not os.path.exists(filepath):
            print("File not found:", filepath)
            continue

        # Read image in RGB format (no need to reorder channels)
        image = skimage.io.imread(filepath)
        torch_image = torch.from_numpy(image).float().permute(2, 0, 1)
        torch_image.unsqueeze_(0)
        torch_image /= 255.0
        torch_image = F.resize(
            torch_image,
            round(IMG_WIDTH / torch_image.shape[-1] * torch_image.shape[-2]),
            interpolation=torchvision.transforms.InterpolationMode.BICUBIC,
        )

        # get image dimensions
        img_height, img_width = torch_image.shape[-2:]

        # get labeled coordinates for object in image
        (
            sign_x1,
            sign_y1,
            sign_x2,
            sign_y2,
            sign_x3,
            sign_y3,
            sign_x4,
            sign_y4,
        ) = (
            float(row["sign_x1"]),
            float(row["sign_y1"]),
            float(row["sign_x2"]),
            float(row["sign_y2"]),
            float(row["sign_x3"]),
            float(row["sign_y3"]),
            float(row["sign_x4"]),
            float(row["sign_y4"]),
        )

        # get labeled coordinates for patch in image
        (
            patch_x1,
            patch_y1,
            patch_x2,
            patch_y2,
            patch_x3,
            patch_y3,
            patch_x4,
            patch_y4,
        ) = (
            float(row["patch_x1"]),
            float(row["patch_y1"]),
            float(row["patch_x2"]),
            float(row["patch_y2"]),
            float(row["patch_x3"]),
            float(row["patch_y3"]),
            float(row["patch_x4"]),
            float(row["patch_y4"]),
        )

        # read in patch and mask from file
        with open(str(patch_path / obj_class / "adv_patch.pkl"), "rb") as file:
            patch, patch_mask = pickle.load(file)

        patch_size_in_pixel = patch.shape[-1]
        hw_ratio_dict = DATASET_METADATA["mapillary-no_color"]["hw_ratio"]
        # get aspect ratio for current object class
        hw_ratio = hw_ratio_dict[(index // 2) % len(traffic_sign_classes)]
        obj_shape = obj_class_to_shape[obj_class]

        # generate mask for object in image
        sign_mask, src = util.gen_sign_mask(
            obj_shape,
            hw_ratio=hw_ratio,
            obj_width_px=round(patch_size_in_pixel * hw_ratio)
            if "rect" in obj_class
            else patch_size_in_pixel,
            pad_to_square=False,
        )
        src = np.array(src).astype(np.float32)

        # get location of patch in canonical sign
        _, hh, ww = np.where(patch_mask.numpy())
        h_min, h_max = hh.min(), hh.max() + 1
        w_min, w_max = ww.min(), ww.max() + 1
        if obj_class == "diamond-s":
            factor = 0.2
        elif obj_class == "diamond-l":
            factor = 0.15
        elif obj_class == "circle":
            factor = 0.1
        elif obj_class == "up-triangle":
            factor = 0.1 * 64 / 56
        else:
            factor = 0.0
        shift = math.ceil(h_max * factor)
        h_min -= shift
        h_max -= shift
        patch_src = np.array(
            [[w_min, h_min], [w_max, h_min], [w_max, h_max], [w_min, h_max]]
        ).astype(np.float32)

        # Shift patch and mask
        patch_mask = torch.zeros_like(patch_mask)
        patch_mask[:, h_min:h_max, w_min:w_max] = 1

        # Get target patch loc if exists
        patch_tgt = None
        if not is_clean:
            patch_tgt = np.array(
                [
                    [patch_x1, patch_y1],
                    [patch_x2, patch_y2],
                    [patch_x3, patch_y3],
                    [patch_x4, patch_y4],
                ]
            ).astype(np.float32)
            patch_tgt *= IMG_WIDTH / 6036

        transform_func = kornia_tf.warp_perspective
        if geo_method in ("affine", "perspective"):
            if len(src) == 3 or geo_method == "affine":
                # Affine transformation (3 points)
                tgt = np.array(
                    [[sign_x1, sign_y1], [sign_x2, sign_y2], [sign_x3, sign_y3]]
                ).astype(np.float32)
                tgt *= IMG_WIDTH / 6036
                sign_tf_matrix = (
                    torch.from_numpy(cv.getAffineTransform(src[:3], tgt))
                    .unsqueeze(0)
                    .float()
                )
                # add [0, 0, 1] to M1
                sign_tf_matrix = torch.cat(
                    (
                        sign_tf_matrix,
                        torch.tensor([0, 0, 1]).view(1, 1, 3).float(),
                    ),
                    dim=1,
                )
            else:
                # Perspective transformation or homography (4 points)
                tgt = np.array(
                    [
                        [sign_x1, sign_y1],
                        [sign_x2, sign_y2],
                        [sign_x3, sign_y3],
                        [sign_x4, sign_y4],
                    ]
                ).astype(np.float32)
                tgt *= IMG_WIDTH / 6036
                src = torch.from_numpy(src).unsqueeze(0)
                tgt = torch.from_numpy(tgt).unsqueeze(0)
                sign_tf_matrix = get_perspective_transform(src, tgt)
                src = src[0]  # unsqueeze(0) above
                tgt = tgt[0]  # unsqueeze(0) above

            # apply perspective transform to src patch coordinates
            patch_src_transformed = cv.perspectiveTransform(
                patch_src.reshape((1, -1, 2)), sign_tf_matrix[0].numpy()
            )[0]
        else:
            # Translate and scale transformation (2 points)
            tgt = np.array(
                [
                    [sign_x1, sign_y1],
                    [sign_x2, sign_y2],
                    [sign_x3, sign_y3],
                    [sign_x4, sign_y4],
                ]
            ).astype(np.float32)
            tgt *= IMG_WIDTH / 6036
            tgt = tgt[: len(src)]
            tgt_center = np.mean(tgt, axis=0)
            src_center = np.mean(src, axis=0)

            def compute_area(x, y):
                return 0.5 * np.abs(
                    np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1))
                )

            tgt_area = compute_area(tgt[:, 0], tgt[:, 1])
            src_area = compute_area(src[:, 0], src[:, 1])
            scale = np.sqrt(tgt_area / src_area)
            patch_src_transformed = (
                patch_src - src_center
            ) * scale + tgt_center

        if SAVE_IMG_DEBUG:
            image_resized = _save_images(
                image,
                src,
                tgt,
                patch_src,
                patch_src_transformed,
                patch_tgt,
                img_width,
                img_height,
                is_clean,
            )
            save_image(image_resized, f"tmp/{index:02d}_test.png")
            # if not is_clean:
            #     save_image(torch_image, f"{data_dir}/real/images/{index:03d}.jpg")

        if is_clean:
            relight_coeffs, syn_obj = compute_relight_params(
                torch_image,
                sign_mask,
                relight_method,
                relight_params,
                obj_class,
                src,
                tgt,
            )
            if isinstance(relight_coeffs, torch.Tensor):
                relight_coeffs = relight_coeffs[None]
            print(f"relight_coeffs: {relight_coeffs}")
            if SAVE_IMG_DEBUG:
                relighted_syn_obj = relight_transform(syn_obj, relight_coeffs)
                save_image(
                    relighted_syn_obj, f"tmp/{index:02d}_relighted_syn_obj.png"
                )
            continue

        # calculate euclidean distance between patch_src_transformed and
        # patch_tgt
        transform_l2_error = np.linalg.norm(
            patch_src_transformed - patch_tgt, axis=1
        ).mean()

        # calculate transform matrix M2 between transformed patch points and
        # labeled patch points
        patch_tf_matrix = get_perspective_transform(
            torch.from_numpy(patch_src).unsqueeze(0),
            torch.from_numpy(patch_tgt).unsqueeze(0),
        )
        transform_func = kornia_tf.warp_perspective

        # apply relighting to transformed synthetic patch
        patch = img_util.coerce_rank(patch, 4)
        patch = relight_transform(patch, relight_coeffs)
        tmp_patch = torch.zeros_like(patch)
        tmp_patch[:, :, h_min:h_max, w_min:w_max] = patch[
            :, :, h_min + shift : h_max + shift, w_min:w_max
        ]
        patch = tmp_patch
        warped_patch = transform_func(
            patch,
            patch_tf_matrix,
            dsize=(img_height, img_width),
            mode="bilinear",
            padding_mode="zeros",
        )
        warped_mask = transform_func(
            patch_mask.unsqueeze(0),
            patch_tf_matrix,
            dsize=(img_height, img_width),
            mode="nearest",
            padding_mode="zeros",
        )
        warped_patch.clamp_(0, 1)

        real_patch = torch.masked_select(torch_image, warped_mask.bool())
        reap_patch = torch.masked_select(warped_patch, warped_mask.bool())
        if SAVE_IMG_DEBUG:
            save_image(warped_patch, f"tmp/{index:02d}_M2_warped_patch.png")
            save_image(warped_mask, f"tmp/{index:02d}_M2_warped_mask.png")
            save_image(
                torch_image * warped_mask, f"tmp/{index:02d}_real_patch.png"
            )
            save_image(warped_patch, f"tmp/{index:02d}_reap_patch.png")
            render_image = (
                1 - warped_mask
            ) * torch_image + warped_mask * warped_patch
            # ymin, xmin, height, width = [int(x) for x in img_util.mask_to_box(warped_mask)]
            if isinstance(tgt, np.ndarray):
                tgt = torch.from_numpy(tgt)
            xmin, ymin = tgt.min(0)[0]
            xmax, ymax = tgt.max(0)[0]
            height, width = int(ymax - ymin), int(xmax - xmin)
            size = max(height, width) * 1.25
            ypad, xpad = round((size - height) // 2), round((size - width) // 2)
            ymin, xmin = int(ymin), int(xmin)
            crop_render = render_image[
                :,
                :,
                max(0, ymin - ypad) : ymin + height + ypad,
                max(0, xmin - xpad) : xmin + width + xpad,
            ]
            crop_orig = torch_image[
                :,
                :,
                max(0, ymin - ypad) : ymin + height + ypad,
                max(0, xmin - xpad) : xmin + width + xpad,
            ]
            # save_image(crop_render, f"tmp/{index:02d}_crop_render.png")
            # save_image(crop_orig, f"tmp/{index:02d}_crop_orig.png")
            crop_both = torch.cat([crop_orig, crop_render], dim=3)
            save_image(render_image, f"{DATA_DIR}/{EXP_NAME}/images/{index:03d}.jpg")
            all_crops.append(crop_both)

        # calculate relighting error between transformed synthetic patch and real patch
        relighting_l2_error = ((real_patch - reap_patch) ** 2).mean().sqrt()

        print()
        print(f"transform_l2_error: {transform_l2_error:.4f}")
        print(f"relighting_l2_error: {relighting_l2_error.item():.4f}")

        geometric_errors.append(transform_l2_error)
        lighting_errors.append(relighting_l2_error.item())

    # plot histogram of geometric_errors and save plot
    plt.hist(geometric_errors, bins=100)
    plt.savefig("tmp/geometric_errors.png")
    plt.clf()

    plt.hist(lighting_errors, bins=100)
    plt.savefig("tmp/relighting_errors.png")
    plt.clf()

    # print statistics for errors
    print("geometric error:")
    print(f"mean: {np.mean(geometric_errors)}")
    print(f"std: {np.std(geometric_errors)}")
    print(f"max: {np.max(geometric_errors)}")
    print(f"min: {np.min(geometric_errors)}")
    print()
    print("lighting error:")
    print(f"mean: {np.mean(lighting_errors)}")
    print(f"std: {np.std(lighting_errors)}")
    print(f"max: {np.max(lighting_errors)}")
    print(f"min: {np.min(lighting_errors)}")

    # Save real vs rendered patches for all images
    num_rows = len(all_crops) // 11
    imgs = [None for _ in all_crops]
    for i, crop in enumerate(all_crops):
        imgs[(i % 11) * 4 + (i // 11)] = img_util.resize_and_pad(
            obj=crop,
            resize_size=all_crops[0].shape[-2:],
            interp="bicubic",
            keep_aspect_ratio=False,
        )[0]
    grid = make_grid(imgs, nrow=num_rows, padding=4, pad_value=1)
    save_image(grid, "tmp/all_crops.png")

    return geometric_errors, lighting_errors


if __name__ == "__main__":
    # flag to control whether to save images for debugging
    DATA_DIR = "~/data/reap-benchmark/reap_realism_test"
    DATA_DIR = os.path.expanduser(DATA_DIR)
    SAVE_IMG_DEBUG = True
    results = {}
    GEO_METHOD = "perspective"  # "translate+scale", "affine", "perspective"

    # RELIGHT_METHOD = "percentile_lab-l"
    # for percentile in range(1, 30):
    #     params = {"percentile": percentile / 100}
    #     results[f"{RELIGHT_METHOD}_{percentile / 100}"] = main(
    #         GEO_METHOD, RELIGHT_METHOD, params
    #     )

    # RELIGHT_METHOD = "polynomial"
    # for drop_topk in [0.0, 0.01, 0.02, 0.05, 0.1, 0.2]:
    #     # for degree in range(4):
    #         params = {"polynomial_degree": degree, "percentile": drop_topk}
    #         results[f"{RELIGHT_METHOD}_p{degree}_k{drop_topk}"] = main(
    #             GEO_METHOD, RELIGHT_METHOD, params
    #         )

    # RELIGHT_METHOD = "color_transfer_lab-l"
    # results[RELIGHT_METHOD] = main(GEO_METHOD, RELIGHT_METHOD, {})

    # RELIGHT_METHOD = "polynomial_max"
    # for drop_topk in [0.0, 0.01, 0.02, 0.05, 0.1, 0.2]:
    #     for degree in range(4):
    #         params = {"polynomial_degree": degree, "percentile": drop_topk}
    #         results[f"{RELIGHT_METHOD}_p{degree}_k{drop_topk}"] = main(
    #             GEO_METHOD, RELIGHT_METHOD, params
    #         )

    # RELIGHT_METHOD = "polynomial_hsv-sv"
    # for drop_topk in [0.0, 0.01, 0.02, 0.05, 0.1, 0.2]:
    #     for degree in range(4):
    #         params = {"polynomial_degree": degree, "percentile": drop_topk}
    #         results[f"{RELIGHT_METHOD}_p{degree}_k{drop_topk}"] = main(
    #             GEO_METHOD, RELIGHT_METHOD, params
    #         )

    # RELIGHT_METHOD = "polynomial_lab"
    # for drop_topk in [0.0, 0.01, 0.02, 0.05, 0.1, 0.2]:
    #     for degree in range(4):
    #         params = {"polynomial_degree": degree, "percentile": drop_topk}
    #         results[f"{RELIGHT_METHOD}_p{degree}_k{drop_topk}"] = main(
    #             GEO_METHOD, RELIGHT_METHOD, params
    #         )

    # RELIGHT_METHOD = "color_transfer_hsv-sv"
    # results[RELIGHT_METHOD] = main(GEO_METHOD, RELIGHT_METHOD, {})

    # RELIGHT_METHOD = "polynomial_hsv-sv"
    # degree, drop_topk = 1, 0.0
    # params = {"polynomial_degree": degree, "percentile": drop_topk}
    # results[f"{RELIGHT_METHOD}_p{degree}_k{drop_topk}"] = main(
    #     GEO_METHOD, RELIGHT_METHOD, params
    # )

    # RELIGHT_METHOD = "percentile"
    # percentile = 0.1
    # params = {"percentile": percentile}
    # EXP_NAME = f"{RELIGHT_METHOD}{percentile}"
    #
    RELIGHT_METHOD = "polynomial"
    params = {"polynomial_degree": 1, "percentile": 0.2}
    EXP_NAME = f"{RELIGHT_METHOD}{params['percentile']}d{params['polynomial_degree']}"
    #
    RELIGHT_METHOD = "color_transfer_lab-l"
    params = {}
    EXP_NAME = "ctlab"
    #
    # RELIGHT_METHOD = "none"
    # params = {}
    # EXP_NAME = "none"

    os.makedirs(f"tmp/{EXP_NAME}/images/", exist_ok=True)
    os.makedirs(f"{DATA_DIR}/{EXP_NAME}/images/", exist_ok=True)
    results[EXP_NAME] = main(
        GEO_METHOD, RELIGHT_METHOD, params, use_jpeg=False
    )

    with open("tmp/realism_test_results.pkl", "wb") as f:
        pickle.dump(results, f)
