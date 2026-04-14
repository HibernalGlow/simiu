from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import imagehash
import numpy as np
import pyperclip
from PIL import Image, UnidentifiedImageError
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
AUTO_GROUP_MARKER = "__set_"
console = Console(highlight=False)


def clean_input_path(raw: str) -> str:
    return raw.strip().strip('"').strip("'").strip()


def parse_clipboard_directories() -> list[Path]:
    try:
        clipboard = pyperclip.paste().strip()
    except Exception:
        return []

    if not clipboard:
        return []

    result: list[Path] = []
    seen: set[Path] = set()
    for line in clipboard.splitlines():
        cleaned = clean_input_path(line)
        if not cleaned:
            continue
        p = Path(cleaned).expanduser()
        if p.exists() and p.is_dir():
            resolved = p.resolve()
            if resolved not in seen:
                result.append(resolved)
                seen.add(resolved)
    return result


def select_clipboard_directory(paths: Sequence[Path]) -> Path | None:
    if not paths:
        return None

    table = Table(title="剪贴板目录预览", box=box.SIMPLE)
    table.add_column("序号", style="cyan", justify="right")
    table.add_column("目录", style="green")
    for idx, p in enumerate(paths, start=1):
        table.add_row(str(idx), escape(str(p)))
    console.print(table)

    if len(paths) == 1:
        use_single = Confirm.ask("是否使用剪贴板中的目录", default=True)
        return paths[0] if use_single else None

    if not Confirm.ask("是否从剪贴板列表中选择目录", default=True):
        return None

    choice = Prompt.ask("输入序号", default="1")
    try:
        index = int(choice)
    except ValueError:
        console.print("[red]无效序号[/red]")
        return None

    if index < 1 or index > len(paths):
        console.print("[red]序号超出范围[/red]")
        return None
    return paths[index - 1]


def prompt_directory_interactive() -> Path | None:
    console.print(
        Panel.fit(
            "支持 3 种输入方式:\n"
            "1) 直接在命令里传路径\n"
            "2) 从剪贴板读取路径\n"
            "3) 终端手动输入路径",
            title="simiu 路径输入",
            border_style="blue",
        )
    )

    clipboard_paths = parse_clipboard_directories()
    selected = select_clipboard_directory(clipboard_paths)
    if selected is not None:
        return selected

    while True:
        raw = Prompt.ask("请输入目录路径", default="")
        cleaned = clean_input_path(raw)
        if not cleaned:
            console.print("[yellow]未输入路径，已取消[/yellow]")
            return None
        p = Path(cleaned).expanduser()
        if not p.exists():
            console.print(f"[red]路径不存在: {escape(str(p))}[/red]")
            continue
        if not p.is_dir():
            console.print(f"[red]不是目录: {escape(str(p))}[/red]")
            continue
        return p.resolve()


def resolve_group_root(args: argparse.Namespace) -> Path | None:
    if args.folder:
        p = Path(clean_input_path(args.folder)).expanduser()
        if not p.exists() or not p.is_dir():
            console.print(f"[red]路径不存在或不是目录: {escape(str(p))}[/red]")
            return None
        return p.resolve()

    if args.clipboard:
        paths = parse_clipboard_directories()
        selected = select_clipboard_directory(paths)
        if selected is None:
            console.print("[red]剪贴板中没有可用目录[/red]")
        return selected

    if not sys.stdin.isatty():
        console.print("[red]未提供目录参数，且当前终端非交互模式。请传入 folder 或 --clipboard[/red]")
        return None

    return prompt_directory_interactive()


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
        return []

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
    for p in valid_paths:
        root = uf.find(p)
        groups.setdefault(root, []).append(p)

    # 对无法读取特征的文件保留为单文件组，后续会被 min-group-size 自动过滤。
    for p in image_paths:
        if p not in features:
            groups[p] = [p]

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
    root = resolve_group_root(args)
    if root is None:
        return 2

    header_text = Text(
        f"目标目录: {root}\n递归遍历: {'是' if args.recursive else '否'}\n扫描顺序: {args.scan_order}"
    )
    console.print(Panel.fit(header_text, title="simiu group", border_style="cyan"))

    folder_batches = collect_folder_batches(root, recursive=args.recursive, scan_order=args.scan_order)
    if not folder_batches:
        console.print("[yellow]未找到图片文件[/yellow]")
        return 0

    groups: list[PlannedGroup] = []
    with console.status("[bold cyan]正在计算相似分组..."):
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
        console.print("[yellow]当前阈值下没有可分组结果[/yellow]")
        return 0

    table = Table(title="分组预览", box=box.SIMPLE_HEAVY)
    table.add_column("序号", style="cyan", justify="right")
    table.add_column("目录", style="magenta")
    table.add_column("分组名", style="green")
    table.add_column("文件数", style="yellow", justify="right")
    table.add_column("示例文件", style="white")

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
        table.add_row(f"{idx:03d}", escape(rel), escape(g.name), str(len(g.files)), escape(f"{sample}{more}"))

    console.print(table)

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
        summary = (
            f"已创建分组目录: {created_groups}\n"
            f"已处理文件: {moved_files}\n"
            f"执行模式: {args.mode}"
        )
        console.print(Panel.fit(summary, title="执行完成", border_style="green"))
        if written_log is not None:
            console.print(f"[green]回滚日志: {escape(str(written_log))}[/green]")
    else:
        summary = (
            f"扫描目录数: {len(folder_batches)}\n"
            f"识别分组数: {len(groups)}\n"
            f"预计处理文件: {total_files}"
        )
        console.print(Panel.fit(summary, title="Dry Run", border_style="blue"))
        console.print("[cyan]提示: 添加 --apply 执行实际落盘[/cyan]")

    return 0


def undo_from_log(args: argparse.Namespace) -> int:
    log_path = Path(args.log_file).expanduser().resolve()
    if not log_path.exists():
        console.print(f"[red]日志文件不存在: {escape(str(log_path))}[/red]")
        return 2

    data = json.loads(log_path.read_text(encoding="utf-8"))
    operations = data.get("operations", [])
    if not isinstance(operations, list):
        console.print("[red]日志格式无效[/red]")
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

    console.print(Panel.fit(f"已回滚操作数: {reverted}", title="Undo 完成", border_style="green"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simiu",
        description="Group variation images by visual similarity within each folder.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    group_parser = sub.add_parser("group", help="Detect and group variations into folders")
    group_parser.add_argument("folder", nargs="?", help="Root folder to process")
    group_parser.add_argument(
        "--clipboard",
        action="store_true",
        help="Read folder path from clipboard when folder is omitted",
    )
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
