from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Sequence

from .models import ImageFeature, PlannedGroup
from .scanner import AUTO_GROUP_MARKER
from .similarity import extract_feature, pair_score


class UnionFind:
    def __init__(self, items: Sequence[Path]) -> None:
        self.parent: dict[Path, Path] = {item: item for item in items}
        self.rank: dict[Path, int] = {item: 0 for item in items}

    def find(self, x: Path) -> Path:
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, a: Path, b: Path) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


def cluster_by_similarity(
    image_paths: Sequence[Path],
    features: dict[Path, ImageFeature],
    threshold: float,
) -> list[list[Path]]:
    valid_paths = [p for p in image_paths if p in features]
    if not valid_paths:
        return []

    # Sort by ratio so we can break early once ratio delta exceeds threshold.
    ordered = sorted(valid_paths, key=lambda p: features[p].ratio)

    uf = UnionFind(valid_paths)
    n = len(ordered)
    for i in range(n):
        a_path = ordered[i]
        a = features[a_path]
        for j in range(i + 1, n):
            b_path = ordered[j]
            b = features[b_path]
            if uf.find(a_path) == uf.find(b_path):
                continue

            ratio_delta = abs(a.ratio - b.ratio)
            if ratio_delta > 0.20:
                break

            score = pair_score(a, b)
            if score <= threshold:
                uf.union(a_path, b_path)

    groups: dict[Path, list[Path]] = {}
    for p in valid_paths:
        root = uf.find(p)
        groups.setdefault(root, []).append(p)

    for p in image_paths:
        if p not in features:
            groups[p] = [p]

    result = [sorted(cluster) for cluster in groups.values()]
    result.sort(key=lambda arr: (len(arr), str(arr[0])), reverse=True)
    return result


def choose_group_name(index: int, prefix: str) -> str:
    return f"{prefix}{AUTO_GROUP_MARKER}{index:03d}"


def dedupe_group_dir_name(parent: Path, name: str, used_names: set[str]) -> str:
    candidate = name
    idx = 1
    while candidate in used_names or (parent / candidate).exists():
        candidate = f"{name}_{idx:02d}"
        idx += 1
    used_names.add(candidate)
    return candidate


def plan_groups_for_folder(
    folder: Path,
    image_paths: Sequence[Path],
    threshold: float,
    min_group_size: int,
    name_prefix: str,
    max_workers: int = 0,
) -> list[PlannedGroup]:
    features: dict[Path, ImageFeature] = {}
    if image_paths:
        if max_workers <= 0:
            cpu = os.cpu_count() or 4
            max_workers = min(16, max(4, cpu), len(image_paths))
        else:
            max_workers = max(1, min(max_workers, len(image_paths)))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for p, f in zip(image_paths, pool.map(extract_feature, image_paths)):
                if f is not None:
                    features[p] = f

    clusters = cluster_by_similarity(image_paths, features, threshold)

    # If all files in this folder collapse into one group, skip to avoid nested "all-in-one" folder.
    if len(clusters) == 1 and len(clusters[0]) == len(image_paths):
        return []

    groups: list[PlannedGroup] = []
    group_index = 1
    used_names: set[str] = set()
    for files in clusters:
        if len(files) < min_group_size:
            continue
        raw_name = choose_group_name(group_index, name_prefix)
        name = dedupe_group_dir_name(folder, raw_name, used_names)
        group_index += 1
        groups.append(PlannedGroup(parent_dir=folder, name=name, files=list(files)))
    return groups
