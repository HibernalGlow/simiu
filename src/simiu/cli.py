from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import imagehash
import numpy as np
from PIL import Image, UnidentifiedImageError

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
AUTO_GROUP_MARKER = "__set_"


@dataclass
class ImageFeature:
    path: Path
    width: int
    height: int
    ratio: float
    mean_rgb: np.ndarray
    dhash_bits: np.ndarray
    file_size: int


@dataclass
class PlannedGroup:
    parent_dir: Path
    name: str
    files: list[Path]


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


def collect_images_in_dir(folder: Path) -> list[Path]:
    files: list[Path] = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            files.append(p)
    return sorted(files)


def folder_depth(root: Path, folder: Path) -> int:
    try:
        return len(folder.relative_to(root).parts)
    except ValueError:
        return len(folder.parts)


def should_skip_directory(folder: Path) -> bool:
    lowered = folder.name.lower()
    if lowered.startswith(".simiu-"):
        return True
    if AUTO_GROUP_MARKER in lowered:
        return True
    return False


def collect_folder_batches(root: Path, recursive: bool, scan_order: str) -> list[tuple[Path, list[Path]]]:
    if not recursive:
        images = collect_images_in_dir(root)
        return [(root, images)] if images else []

    dirs = [root]
    dirs.extend(sorted(p for p in root.rglob("*") if p.is_dir()))

    batches: list[tuple[Path, list[Path]]] = []
    for folder in dirs:
        if should_skip_directory(folder):
            continue
        images = collect_images_in_dir(folder)
        if images:
            batches.append((folder, images))

    if scan_order == "smallest-first":
        batches.sort(key=lambda x: (len(x[1]), -folder_depth(root, x[0]), str(x[0]).lower()))
    elif scan_order == "deepest-first":
        batches.sort(key=lambda x: (-folder_depth(root, x[0]), len(x[1]), str(x[0]).lower()))
    else:
        batches.sort(key=lambda x: str(x[0]).lower())
    return batches


def extract_feature(path: Path) -> ImageFeature | None:
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            width, height = img.size
            if width == 0 or height == 0:
                return None
            ratio = width / float(height)

            small = img.resize((32, 32), Image.Resampling.BILINEAR)
            small_arr = np.asarray(small, dtype=np.uint8)
            mean_rgb = small_arr.reshape(-1, 3).mean(axis=0)
            dh = imagehash.dhash(img, hash_size=16)
            dh_bits = np.asarray(dh.hash, dtype=np.uint8).flatten()
            fsize = path.stat().st_size
            return ImageFeature(
                path=path,
                width=width,
                height=height,
                ratio=ratio,
                mean_rgb=mean_rgb,
                dhash_bits=dh_bits,
                file_size=fsize,
            )
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def hamming_distance(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 1.0
    return float(np.count_nonzero(a != b)) / float(a.size)


def color_distance(a: np.ndarray, b: np.ndarray) -> float:
    max_norm = (255.0 * (3.0 ** 0.5))
    return float(np.linalg.norm(a - b) / max_norm)


def file_size_distance(a: int, b: int) -> float:
    if a <= 0 or b <= 0:
        return 1.0
    mn = min(a, b)
    mx = max(a, b)
    return 1.0 - (mn / mx)


def pair_score(a: ImageFeature, b: ImageFeature) -> float:
    hash_dist = hamming_distance(a.dhash_bits, b.dhash_bits)
    ratio_dist = min(abs(a.ratio - b.ratio), 1.0)
    color_dist = color_distance(a.mean_rgb, b.mean_rgb)
    size_dist = file_size_distance(a.file_size, b.file_size)
    return 0.62 * hash_dist + 0.18 * ratio_dist + 0.12 * color_dist + 0.08 * size_dist


def cluster_by_similarity(
    image_paths: Sequence[Path],
    features: dict[Path, ImageFeature],
    threshold: float,
) -> list[list[Path]]:
    valid_paths = [p for p in image_paths if p in features]
    if not valid_paths:
        return [list(image_paths)]

    uf = UnionFind(valid_paths)
    n = len(valid_paths)
    for i in range(n):
        a_path = valid_paths[i]
        a = features[a_path]
        for j in range(i + 1, n):
            b_path = valid_paths[j]
            b = features[b_path]
            if uf.find(a_path) == uf.find(b_path):
                continue

            if a.height == 0 or b.height == 0:
                continue
            ratio_delta = abs(a.ratio - b.ratio)
            if ratio_delta > 0.20:
                continue

            score = pair_score(a, b)
            if score <= threshold:
                uf.union(a_path, b_path)

    groups: dict[Path, list[Path]] = {}
    for p in image_paths:
        root = uf.find(p)
        groups.setdefault(root, []).append(p)

    result = []
    for cluster in groups.values():
        result.append(sorted(cluster))
    result.sort(key=lambda arr: (len(arr), str(arr[0])), reverse=True)
    return result


def choose_group_name(files: Sequence[Path], index: int) -> str:
    _ = files
    return f"simiu_set{AUTO_GROUP_MARKER}{index:03d}"


def dedupe_group_dir_name(parent: Path, name: str, used_names: set[str]) -> str:
    candidate = name
    idx = 1
    while candidate in used_names or (parent / candidate).exists():
        candidate = f"{name}_{idx:02d}"
        idx += 1
    used_names.add(candidate)
    return candidate


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def plan_groups_for_folder(
    folder: Path,
    image_paths: Sequence[Path],
    threshold: float,
    min_group_size: int,
) -> list[PlannedGroup]:
    features: dict[Path, ImageFeature] = {}
    for p in image_paths:
        f = extract_feature(p)
        if f is not None:
            features[p] = f

    clusters = cluster_by_similarity(image_paths, features, threshold)
    groups: list[PlannedGroup] = []
    group_index = 1
    used_names: set[str] = set()
    for files in clusters:
        if len(files) < min_group_size:
            continue
        raw_name = choose_group_name(files, group_index)
        name = dedupe_group_dir_name(folder, raw_name, used_names)
        group_index += 1
        groups.append(PlannedGroup(parent_dir=folder, name=name, files=list(files)))
    return groups


def apply_groups(
    root: Path,
    groups: Sequence[PlannedGroup],
    mode: str,
    apply: bool,
    undo_log: Path | None,
) -> tuple[int, int, Path | None]:
    moved_files = 0
    created_groups = 0
    operations: list[dict[str, str]] = []

    for group in groups:
        group_dir = group.parent_dir / group.name
        if not group_dir.exists() and apply:
            group_dir.mkdir(parents=True, exist_ok=True)
        if apply:
            created_groups += 1

        for src in group.files:
            dst = group_dir / src.name
            dst = ensure_unique_path(dst)
            if not apply:
                moved_files += 1
                continue

            if mode == "move":
                shutil.move(str(src), str(dst))
            elif mode == "copy":
                shutil.copy2(src, dst)
            elif mode == "link":
                os.link(src, dst)
            else:
                raise ValueError(f"Unsupported mode: {mode}")

            moved_files += 1
            operations.append({"mode": mode, "src": str(src), "dst": str(dst)})

    written_log: Path | None = None
    if apply and undo_log is not None:
        payload = {
            "created_at": datetime.now().isoformat(),
            "root": str(root),
            "operations": operations,
        }
        undo_log.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        written_log = undo_log

    return moved_files, created_groups, written_log


def run_group(args: argparse.Namespace) -> int:
    root = Path(args.folder).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        print(f"[error] folder not found: {root}")
        return 2

    folder_batches = collect_folder_batches(root, recursive=args.recursive, scan_order=args.scan_order)
    if not folder_batches:
        print("[info] no image file found")
        return 0

    groups: list[PlannedGroup] = []
    for folder, image_paths in folder_batches:
        groups.extend(
            plan_groups_for_folder(
                folder=folder,
                image_paths=image_paths,
                threshold=args.threshold,
                min_group_size=args.min_group_size,
            )
        )

    if not groups:
        print("[info] no candidate groups found under current threshold")
        return 0

    print(f"[preview] scanned folders: {len(folder_batches)}, detected groups: {len(groups)}")
    total_files = 0
    show_limit = args.preview_limit
    for idx, g in enumerate(groups, start=1):
        total_files += len(g.files)
        sample = ", ".join(f.name for f in g.files[:show_limit])
        more = "" if len(g.files) <= show_limit else f" ... +{len(g.files) - show_limit}"
        try:
            rel = "." if g.parent_dir == root else str(g.parent_dir.relative_to(root))
        except ValueError:
            rel = str(g.parent_dir)
        print(f"  {idx:03d}. [{rel}] {g.name} ({len(g.files)} files): {sample}{more}")

    apply_flag = args.apply
    undo_log = None
    if apply_flag:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        undo_log = root / f".simiu-undo-{stamp}.json"

    moved_files, created_groups, written_log = apply_groups(
        root=root,
        groups=groups,
        mode=args.mode,
        apply=apply_flag,
        undo_log=undo_log,
    )

    if apply_flag:
        print(f"[done] groups created: {created_groups}, processed files: {moved_files}, mode: {args.mode}")
        if written_log is not None:
            print(f"[done] undo log: {written_log}")
    else:
        print(f"[dry-run] would process files: {total_files}")
        print("[hint] add --apply to execute")

    return 0


def undo_from_log(args: argparse.Namespace) -> int:
    log_path = Path(args.log_file).expanduser().resolve()
    if not log_path.exists():
        print(f"[error] log file not found: {log_path}")
        return 2

    data = json.loads(log_path.read_text(encoding="utf-8"))
    operations = data.get("operations", [])
    if not isinstance(operations, list):
        print("[error] invalid log format")
        return 2

    reverted = 0
    for op in reversed(operations):
        mode = op.get("mode")
        src = Path(op.get("src", ""))
        dst = Path(op.get("dst", ""))

        if mode == "move":
            if dst.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                target = ensure_unique_path(src)
                shutil.move(str(dst), str(target))
                reverted += 1
        elif mode in {"copy", "link"}:
            if dst.exists():
                dst.unlink()
                reverted += 1

    if args.clean_empty_dirs:
        for op in operations:
            dst = Path(op.get("dst", ""))
            parent = dst.parent
            if parent.exists() and parent.is_dir():
                try:
                    parent.rmdir()
                except OSError:
                    pass

    print(f"[done] reverted operations: {reverted}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simiu",
        description="Group variation images by visual similarity within each folder.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    group_parser = sub.add_parser("group", help="Detect and group variations into folders")
    group_parser.add_argument("folder", help="Root folder to process")
    group_parser.add_argument(
        "--recursive",
        action="store_true",
        help="Traverse sub-folders, but only cluster files inside the same folder",
    )
    group_parser.add_argument(
        "--scan-order",
        choices=["smallest-first", "deepest-first", "natural"],
        default="smallest-first",
        help="Folder scan order when using --recursive",
    )
    group_parser.add_argument("--threshold", type=float, default=0.17, help="Similarity threshold, lower is stricter")
    group_parser.add_argument("--min-group-size", type=int, default=2, help="Minimum files needed for a group")
    group_parser.add_argument("--preview-limit", type=int, default=5, help="Show N filenames per group in preview")
    group_parser.add_argument("--apply", action="store_true", help="Apply changes to filesystem")
    group_parser.add_argument(
        "--mode",
        choices=["move", "copy", "link"],
        default="move",
        help="How to place files into group folders when using --apply",
    )
    group_parser.set_defaults(func=run_group)

    undo_parser = sub.add_parser("undo", help="Undo operations using undo log file")
    undo_parser.add_argument("log_file", help="Path to .simiu-undo-*.json")
    undo_parser.add_argument(
        "--clean-empty-dirs",
        action="store_true",
        help="Try removing now-empty group directories",
    )
    undo_parser.set_defaults(func=undo_from_log)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
