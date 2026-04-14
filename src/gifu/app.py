from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import time
from typing import Iterable
import tarfile
import zipfile

import typer
from PIL import Image
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from .config import load_config

try:
    import pyperclip
except Exception:  # noqa: BLE001
    pyperclip = None

IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".gif",
    ".avif",
    ".jxl",
}
ARCHIVE_EXTS = {
    ".zip",
    ".cbz",
    ".tar",
    ".tgz",
    ".tar.gz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
}
ANIM_FORMATS = {"gif", "webp", "apng"}

console = Console(highlight=False)
app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="Batch convert archives to animated gif/webp/apng by internal file order.",
)


@dataclass(frozen=True)
class ConvertResult:
    archive_path: Path
    output_path: Path
    frame_count: int


@app.callback(invoke_without_command=True)
def entry(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _interactive_entry()
        raise typer.Exit(0)


def _clean_line_path(line: str) -> str:
    cleaned = line.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _parse_paths_from_text(text: str) -> list[Path]:
    result: list[Path] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cleaned = _clean_line_path(line)
        if cleaned:
            result.append(Path(cleaned))
    return result


def _is_archive_file(path: Path) -> bool:
    low = path.name.lower()
    return any(low.endswith(ext) for ext in ARCHIVE_EXTS)


def _sanitize_output_stem(stem: str) -> str:
    banned = '<>:"/\\|?*'
    cleaned = stem
    for ch in banned:
        cleaned = cleaned.replace(ch, "_")
    cleaned = cleaned.strip().strip(".")
    return cleaned or "output"


def _render_output_stem(archive_path: Path, template: str, prefix: str) -> str:
    try:
        rendered = template.format(
            prefix=prefix,
            stem=archive_path.stem,
            archive=archive_path.name,
            parent=archive_path.parent.name,
        )
    except Exception:
        rendered = f"{prefix}{archive_path.stem}"
    return _sanitize_output_stem(rendered)


def _parse_list_file(path: Path) -> list[Path]:
    if not path.exists() or not path.is_file():
        raise ValueError(f"路径清单文件不存在: {path}")

    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = path.read_text(encoding=enc)
            return _parse_paths_from_text(text)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法读取路径清单编码: {path}")


def _parse_clipboard_paths() -> list[Path]:
    if pyperclip is None:
        return []

    try:
        text = pyperclip.paste().strip()
    except Exception:
        return []

    if not text:
        return []

    return _parse_paths_from_text(text)


def _collect_archives(paths: Iterable[Path], recursive: bool) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()

    for raw in paths:
        p = raw.expanduser().resolve()
        if not p.exists():
            continue

        if p.is_file() and _is_archive_file(p):
            if p not in seen:
                seen.add(p)
                result.append(p)
            continue

        if p.is_dir():
            iterator = p.rglob("*") if recursive else p.glob("*")
            for child in sorted(iterator):
                if not child.is_file() or not _is_archive_file(child):
                    continue
                resolved = child.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                result.append(resolved)

    return result


def _iter_zip_images(archive_path: Path):
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            inner = Path(info.filename)
            if inner.suffix.lower() not in IMAGE_EXTS:
                continue
            with zf.open(info, "r") as fp:
                yield fp.read()


def _iter_tar_images(archive_path: Path):
    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            inner = Path(member.name)
            if inner.suffix.lower() not in IMAGE_EXTS:
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            extracted.close()
            yield data


def _load_frames_by_internal_order(archive_path: Path) -> list[Image.Image]:
    low = archive_path.name.lower()
    if low.endswith(".zip") or low.endswith(".cbz"):
        data_iter = _iter_zip_images(archive_path)
    elif any(
        low.endswith(ext)
        for ext in (".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
    ):
        data_iter = _iter_tar_images(archive_path)
    else:
        raise ValueError(f"不支持的压缩包格式: {archive_path}")

    frames: list[Image.Image] = []
    for data in data_iter:
        try:
            with Image.open(BytesIO(data)) as im:
                frames.append(im.convert("RGBA").copy())
        except Exception:
            # Ignore unreadable image entries and continue.
            continue
    return frames


def _normalize_canvas(frames: list[Image.Image]) -> list[Image.Image]:
    max_w = max(f.width for f in frames)
    max_h = max(f.height for f in frames)
    normalized: list[Image.Image] = []

    for frame in frames:
        if frame.width == max_w and frame.height == max_h:
            normalized.append(frame)
            continue
        canvas = Image.new("RGBA", (max_w, max_h), (0, 0, 0, 0))
        offset = ((max_w - frame.width) // 2, (max_h - frame.height) // 2)
        canvas.alpha_composite(frame, dest=offset)
        normalized.append(canvas)

    return normalized


def _convert_one_archive(
    archive_path: Path,
    output_path: Path,
    anim_format: str,
    duration_ms: int,
    loop: int,
    quality: int,
    webp_method: int,
    overwrite: bool,
) -> ConvertResult:
    fmt = anim_format.lower()
    if fmt == "auto":
        fmt = "webp"
    if fmt not in ANIM_FORMATS:
        raise ValueError(f"不支持的输出格式: {anim_format}")

    if output_path.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在: {output_path}")

    frames = _load_frames_by_internal_order(archive_path)
    if len(frames) < 2:
        raise ValueError("可用图片帧少于 2，无法生成动图")

    frames = _normalize_canvas(frames)
    first, rest = frames[0], frames[1:]

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "gif":
        first.save(
            output_path,
            format="GIF",
            save_all=True,
            append_images=rest,
            duration=duration_ms,
            loop=loop,
            disposal=2,
            optimize=False,
        )
    elif fmt == "webp":
        first.save(
            output_path,
            format="WEBP",
            save_all=True,
            append_images=rest,
            duration=duration_ms,
            loop=loop,
            quality=quality,
            method=webp_method,
            lossless=False,
        )
    else:
        first.save(
            output_path,
            format="PNG",
            save_all=True,
            append_images=rest,
            duration=duration_ms,
            loop=loop,
            optimize=False,
        )

    return ConvertResult(
        archive_path=archive_path,
        output_path=output_path,
        frame_count=len(frames),
    )


def _resolve_max_workers(max_workers: int | None, config_workers: int, task_count: int) -> int:
    if task_count <= 1:
        return 1
    if max_workers is not None:
        if max_workers == 0:
            auto = min(32, (os.cpu_count() or 4) + 4)
            return max(1, min(auto, task_count))
        return 1 if max_workers <= 1 else min(max_workers, task_count)
    if config_workers > 0:
        return min(config_workers, task_count)
    auto = min(32, (os.cpu_count() or 4) + 4)
    return max(1, min(auto, task_count))


def _build_output_path(archive_path: Path, output_root: Path | None, target_ext: str, template: str, prefix: str) -> Path:
    out_stem = _render_output_stem(archive_path, template, prefix)
    if output_root is not None:
        return output_root / f"{out_stem}{target_ext}"
    return archive_path.with_name(f"{out_stem}{target_ext}")


def _run_make(
    archives: list[str],
    list_file: str | None,
    clipboard: bool,
    recursive: bool,
    out_dir: str | None,
    fmt: str | None,
    duration_ms: int | None,
    loop: int | None,
    quality: int | None,
    webp_method: int | None,
    name_prefix: str | None,
    name_template: str | None,
    max_workers: int | None,
    config: str | None,
    overwrite: bool,
) -> None:
    app_config = load_config(config)
    if app_config.source_path is not None:
        console.print(f"[blue]已加载配置: {escape(str(app_config.source_path))}[/blue]")

    effective_fmt = (fmt or app_config.output.format).lower()
    effective_duration_ms = duration_ms if duration_ms is not None else app_config.output.duration_ms
    effective_loop = loop if loop is not None else app_config.output.loop
    effective_quality = quality if quality is not None else app_config.output.quality
    effective_webp_method = webp_method if webp_method is not None else app_config.output.webp_method
    effective_prefix = name_prefix if name_prefix is not None else app_config.naming.prefix
    effective_template = name_template if name_template is not None else app_config.naming.template

    if "{stem}" not in effective_template:
        effective_template = f"{effective_template}{{stem}}"

    fmt = effective_fmt
    if fmt not in {"gif", "webp", "apng", "auto"}:
        console.print("[red]format 仅支持: gif, webp, apng, auto[/red]")
        raise typer.Exit(2)
    if effective_duration_ms <= 0:
        console.print("[red]duration 必须大于 0[/red]")
        raise typer.Exit(2)
    if effective_loop < 0:
        console.print("[red]loop 必须大于等于 0[/red]")
        raise typer.Exit(2)
    if effective_quality < 1 or effective_quality > 100:
        console.print("[red]quality 必须在 1-100[/red]")
        raise typer.Exit(2)
    if effective_webp_method < 0 or effective_webp_method > 6:
        console.print("[red]webp-method 必须在 0-6[/red]")
        raise typer.Exit(2)

    raw_inputs: list[Path] = [Path(p) for p in archives]
    if archives:
        console.print(f"[blue]参数输入路径: {len(archives)} 条[/blue]")

    if list_file:
        try:
            list_paths = _parse_list_file(Path(list_file).expanduser().resolve())
            raw_inputs.extend(list_paths)
            console.print(f"[blue]清单读取路径: {len(list_paths)} 条[/blue]")
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(2)

    if clipboard:
        if pyperclip is None:
            console.print("[yellow]未安装 pyperclip，已忽略 --clipboard[/yellow]")
        clipboard_paths = _parse_clipboard_paths()
        raw_inputs.extend(clipboard_paths)
        console.print(f"[blue]剪贴板读取路径: {len(clipboard_paths)} 条[/blue]")

    if not raw_inputs:
        console.print("[red]请至少提供一个压缩包路径（参数/--list-file/--clipboard）[/red]")
        raise typer.Exit(2)

    console.print(f"[blue]合计输入路径: {len(raw_inputs)} 条，开始扫描压缩包...[/blue]")
    archives_found = _collect_archives(raw_inputs, recursive=recursive)
    if not archives_found:
        console.print("[yellow]未找到可处理压缩包（支持 zip/cbz/tar/tgz/tar.gz/tbz2/txz）[/yellow]")
        raise typer.Exit(0)

    console.print(f"[blue]扫描完成，可处理压缩包: {len(archives_found)} 个[/blue]")

    target_ext = ".webp" if fmt == "auto" else f".{fmt}"
    output_root = Path(out_dir).expanduser().resolve() if out_dir else None
    workers = _resolve_max_workers(max_workers, app_config.performance.max_workers, len(archives_found))
    console.print(f"[blue]并行线程: {workers}[/blue]")

    ok = 0
    failed = 0
    total_frames = 0
    started = time.perf_counter()
    if workers == 1:
        for archive_path in archives_found:
            output_path = _build_output_path(archive_path, output_root, target_ext, effective_template, effective_prefix)
            try:
                result = _convert_one_archive(
                    archive_path=archive_path,
                    output_path=output_path,
                    anim_format=fmt,
                    duration_ms=effective_duration_ms,
                    loop=effective_loop,
                    quality=effective_quality,
                    webp_method=effective_webp_method,
                    overwrite=overwrite,
                )
                ok += 1
                total_frames += result.frame_count
                console.print(
                    f"[green]完成[/green] {escape(str(result.archive_path))} -> "
                    f"{escape(str(result.output_path))} ({result.frame_count} 帧)"
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                console.print(f"[red]失败[/red] {escape(str(archive_path))}: {escape(str(exc))}")
    else:
        future_map = {}
        with ProcessPoolExecutor(max_workers=workers) as executor:
            for archive_path in archives_found:
                output_path = _build_output_path(archive_path, output_root, target_ext, effective_template, effective_prefix)
                fut = executor.submit(
                    _convert_one_archive,
                    archive_path,
                    output_path,
                    fmt,
                    effective_duration_ms,
                    effective_loop,
                    effective_quality,
                    effective_webp_method,
                    overwrite,
                )
                future_map[fut] = archive_path

            for fut in as_completed(future_map):
                archive_path = future_map[fut]
                try:
                    result = fut.result()
                    ok += 1
                    total_frames += result.frame_count
                    console.print(
                        f"[green]完成[/green] {escape(str(result.archive_path))} -> "
                        f"{escape(str(result.output_path))} ({result.frame_count} 帧)"
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    console.print(f"[red]失败[/red] {escape(str(archive_path))}: {escape(str(exc))}")

    elapsed = max(0.0001, time.perf_counter() - started)
    fps = total_frames / elapsed
    console.print(f"[cyan]性能: {total_frames} 帧 / {elapsed:.2f}s = {fps:.2f} 帧/s[/cyan]")
    console.print(f"[cyan]处理完成: 成功 {ok}，失败 {failed}，总计 {len(archives_found)}[/cyan]")


def _interactive_entry() -> None:
    console.print(
        Panel.fit(
            "无参数启动已进入交互模式\n"
            "支持来源: 手动输入路径列表 / 清单文件 / 剪贴板路径列表",
            title="gifu 交互引导",
            border_style="cyan",
        )
    )

    use_clipboard_default = bool(_parse_clipboard_paths())
    use_clipboard = Confirm.ask("是否读取剪贴板中的路径列表", default=use_clipboard_default)

    config_path_raw = Prompt.ask("配置文件路径（留空自动查找 gifu.toml）", default="")
    config_path = _clean_line_path(config_path_raw) or None
    app_config = load_config(config_path)
    if app_config.source_path is not None:
        console.print(f"[blue]已加载配置: {escape(str(app_config.source_path))}[/blue]")

    list_file: str | None = None
    if Confirm.ask("是否使用路径清单文件（每行一个路径）", default=False):
        list_file_raw = Prompt.ask("请输入清单文件路径", default="")
        list_file = _clean_line_path(list_file_raw) or None

    manual_raw = Prompt.ask("请输入路径列表（多个用 ; 分隔，可留空）", default="")
    archives = [p for p in (_clean_line_path(x) for x in manual_raw.split(";")) if p]

    recursive = Confirm.ask("输入包含目录时是否递归查找压缩包", default=True)
    fmt = Prompt.ask(
        "输出格式",
        choices=["gif", "webp", "apng", "auto"],
        default=app_config.output.format,
    )
    duration_ms = int(Prompt.ask("每帧时长毫秒", default=str(app_config.output.duration_ms)))
    loop = int(Prompt.ask("循环次数（0 为无限）", default=str(app_config.output.loop)))
    quality = int(Prompt.ask("webp 质量（1-100）", default=str(app_config.output.quality)))
    webp_method = int(Prompt.ask("webp 编码速度档位（0-6，越低越快）", default=str(app_config.output.webp_method)))
    workers = int(Prompt.ask("并行线程数（0 自动）", default=str(app_config.performance.max_workers)))
    name_prefix = Prompt.ask("输出名前缀", default=app_config.naming.prefix)
    name_template = Prompt.ask("命名模板（可用 {prefix} {stem} {archive} {parent}）", default=app_config.naming.template)
    overwrite = Confirm.ask("输出已存在时是否覆盖", default=False)

    out_dir_raw = Prompt.ask("输出目录（留空则与原压缩包同目录）", default="")
    out_dir = _clean_line_path(out_dir_raw) or None

    _run_make(
        archives=archives,
        list_file=list_file,
        clipboard=use_clipboard,
        recursive=recursive,
        out_dir=out_dir,
        fmt=fmt,
        duration_ms=duration_ms,
        loop=loop,
        quality=quality,
        webp_method=webp_method,
        name_prefix=name_prefix,
        name_template=name_template,
        max_workers=workers,
        config=config_path,
        overwrite=overwrite,
    )


@app.command("make")
def make_command(
    archives: list[str] = typer.Argument(None, help="压缩包路径，支持多个；也可传目录"),
    list_file: str | None = typer.Option(None, "--list-file", help="路径清单文件（每行一个路径）"),
    clipboard: bool = typer.Option(False, "--clipboard", help="从剪贴板读取路径列表（每行一个）"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="输入目录时是否递归查找压缩包"),
    out_dir: str | None = typer.Option(None, "--out-dir", help="输出目录，不传则输出到各压缩包同目录"),
    config: str | None = typer.Option(None, "--config", help="gifu 配置文件路径"),
    fmt: str | None = typer.Option(None, "--format", help="输出格式: gif|webp|apng|auto", case_sensitive=False),
    duration_ms: int | None = typer.Option(None, "--duration", help="每帧时长（毫秒），默认读取配置"),
    loop: int | None = typer.Option(None, "--loop", help="循环次数，0 为无限，默认读取配置"),
    quality: int | None = typer.Option(None, "--quality", help="webp 质量（1-100）"),
    webp_method: int | None = typer.Option(None, "--webp-method", help="webp 编码档位 0-6，越低越快"),
    max_workers: int | None = typer.Option(None, "--max-workers", help="并行线程数，默认读取配置，0 自动"),
    name_prefix: str | None = typer.Option(None, "--name-prefix", help="输出名前缀，默认来自配置"),
    name_template: str | None = typer.Option(None, "--name-template", help="命名模板，默认来自配置"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已存在的输出文件"),
) -> None:
    _run_make(
        archives=archives,
        list_file=list_file,
        clipboard=clipboard,
        recursive=recursive,
        out_dir=out_dir,
        config=config,
        fmt=fmt,
        duration_ms=duration_ms,
        loop=loop,
        quality=quality,
        webp_method=webp_method,
        name_prefix=name_prefix,
        name_template=name_template,
        max_workers=max_workers,
        overwrite=overwrite,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
