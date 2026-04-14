from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class ImageFeature:
    path: Path
    width: int
    height: int
    ratio: float
    mean_rgb: np.ndarray
    phash_bits: np.ndarray
    file_size: int


@dataclass
class PlannedGroup:
    parent_dir: Path
    name: str
    files: list[Path]
