from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .models import ImageFeature


def phash_bits_from_bgr(img_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(resized))
    dct_low = dct[:8, :8]

    # Ignore DC component when computing the threshold.
    med = np.median(dct_low[1:, 1:])
    bits = (dct_low > med).astype(np.uint8).flatten()
    return bits


def extract_feature(path: Path) -> ImageFeature | None:
    try:
        arr = np.fromfile(str(path), dtype=np.uint8)
        if arr.size == 0:
            return None
        img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img_bgr is None:
            return None

        height, width = img_bgr.shape[:2]
        if width == 0 or height == 0:
            return None

        ratio = width / float(height)
        small = cv2.resize(img_bgr, (32, 32), interpolation=cv2.INTER_AREA)
        small_rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        mean_rgb = small_rgb.reshape(-1, 3).mean(axis=0)
        bits = phash_bits_from_bgr(img_bgr)
        fsize = path.stat().st_size

        return ImageFeature(
            path=path,
            width=width,
            height=height,
            ratio=ratio,
            mean_rgb=mean_rgb,
            phash_bits=bits,
            file_size=fsize,
        )
    except (OSError, ValueError, cv2.error):
        return None


def hamming_distance(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 1.0
    return float(np.count_nonzero(a != b)) / float(a.size)


def color_distance(a: np.ndarray, b: np.ndarray) -> float:
    max_norm = 255.0 * (3.0 ** 0.5)
    return float(np.linalg.norm(a - b) / max_norm)


def file_size_distance(a: int, b: int) -> float:
    if a <= 0 or b <= 0:
        return 1.0
    mn = min(a, b)
    mx = max(a, b)
    return 1.0 - (mn / mx)


def pair_score(a: ImageFeature, b: ImageFeature) -> float:
    hash_dist = hamming_distance(a.phash_bits, b.phash_bits)
    ratio_dist = min(abs(a.ratio - b.ratio), 1.0)
    color_dist = color_distance(a.mean_rgb, b.mean_rgb)
    size_dist = file_size_distance(a.file_size, b.file_size)
    return 0.68 * hash_dist + 0.14 * ratio_dist + 0.10 * color_dist + 0.08 * size_dist
