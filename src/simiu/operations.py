from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .models import PlannedGroup


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


def apply_groups(
    root: Path,
    groups: Sequence[PlannedGroup],
    mode: str,
    apply: bool,
) -> tuple[int, int, Path | None]:
    moved_files = 0
    created_groups = 0
    operations: list[dict[str, str]] = []

    undo_log: Path | None = None
    if apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        undo_log = root / f".simiu-undo-{stamp}.json"

    for group in groups:
        group_dir = group.parent_dir / group.name
        if not group_dir.exists() and apply:
            group_dir.mkdir(parents=True, exist_ok=True)
        if apply:
            created_groups += 1

        for src in group.files:
            dst = ensure_unique_path(group_dir / src.name)
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

    if apply and undo_log is not None:
        payload = {
            "created_at": datetime.now().isoformat(),
            "root": str(root),
            "operations": operations,
        }
        undo_log.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    return moved_files, created_groups, undo_log


def undo_from_log(log_file: Path, clean_empty_dirs: bool) -> int:
    data = json.loads(log_file.read_text(encoding="utf-8"))
    operations = data.get("operations", [])
    if not isinstance(operations, list):
        raise ValueError("invalid log format")

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

    if clean_empty_dirs:
        for op in operations:
            dst = Path(op.get("dst", ""))
            parent = dst.parent
            if parent.exists() and parent.is_dir():
                try:
                    parent.rmdir()
                except OSError:
                    pass

    return reverted
