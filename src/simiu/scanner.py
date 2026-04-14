from __future__ import annotations

from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
AUTO_GROUP_MARKER = "__set_"


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


def has_images_in_children(root: Path) -> bool:
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            return True
    return False
