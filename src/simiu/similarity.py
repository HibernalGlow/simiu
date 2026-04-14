from __future__ import annotations

from pathlib import Path
import threading

import cv2
import imagehash
import numpy as np
from PIL import Image

from .models import ImageFeature

_TLS = threading.local()


def _opencv_phash_bits(gray_img: np.ndarray) -> np.ndarray | None:
    if not hasattr(cv2, "img_hash"):
        return None
    try:
        hasher = getattr(_TLS, "opencv_phash", None)
        if hasher is None:
            hasher = cv2.img_hash.PHash_create()
            _TLS.opencv_phash = hasher

        digest = hasher.compute(gray_img)
        if digest is None:
            return None
        bits = np.unpackbits(np.asarray(digest, dtype=np.uint8).flatten())
        return bits.astype(np.uint8)
    except Exception:
        return None


def phash_bits_from_bgr(img_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    # Prefer OpenCV native pHash for speed; fallback to imagehash for compatibility.
    fast_bits = _opencv_phash_bits(gray)
    if fast_bits is not None and fast_bits.size > 0:
        return fast_bits

    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    pil_image = Image.fromarray(rgb)
    ph = imagehash.phash(pil_image, hash_size=16)
    return np.asarray(ph.hash, dtype=np.uint8).flatten()


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
    except Exception:
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
