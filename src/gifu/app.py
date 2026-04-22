from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess
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


def _load_pillow_codecs() -> None:
    # Try common module names used by Pillow AVIF/JXL plugins.
    for module_name in ("pillow_avif", "pillow_avif_plugin"):
        try:
            __import__(module_name)
            break
        except Exception:
            continue

    for module_name in ("pillow_jxl", "pillow_jxl_plugin"):
        try:
            __import__(module_name)
            break
        except Exception:
            continue


_load_pillow_codecs()

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
OUTPUT_FORMATS = {"gif", "webp", "apng", "webm", "mp4"}

console = Console(highlight=False)
app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="Batch convert archives to animated gif/webp/apng/webm/mp4 by internal file order.",
)


@dataclass(frozen=True)
class ConvertResult:
    archive_path: Path
    output_path: Path
    frame_count: int
    skipped_frames: int


class SkipArchiveError(Exception):
    """压缩包因合理原因被跳过（如单图、无有效帧等），不算失败。"""


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
            try:
                with zf.open(info, "r") as fp:
                    yield fp.read()
            except Exception:
                # Skip corrupted entries and keep processing remaining frames.
                continue


def _iter_tar_images(archive_path: Path):
    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            inner = Path(member.name)
            if inner.suffix.lower() not in IMAGE_EXTS:
                continue
            try:
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read()
                extracted.close()
                yield data
            except Exception:
                # Skip corrupted entries and keep processing remaining frames.
                continue


def _count_image_entries(archive_path: Path) -> int:
    """快速统计压缩包内图片文件数量（不解码内容，仅按扩展名判断）。"""
    low = archive_path.name.lower()
    count = 0
    if low.endswith(".zip") or low.endswith(".cbz"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if Path(info.filename).suffix.lower() in IMAGE_EXTS:
                    count += 1
    elif any(
        low.endswith(ext)
        for ext in (".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
    ):
        with tarfile.open(archive_path, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                if Path(member.name).suffix.lower() in IMAGE_EXTS:
                    count += 1
    return count


def _load_frames_by_internal_order(archive_path: Path) -> tuple[list[Image.Image], int]:
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
    skipped_frames = 0
    for data in data_iter:
        try:
            with Image.open(BytesIO(data)) as im:
                frames.append(im.convert("RGBA").copy())
        except Exception:
            # Ignore unreadable image entries and continue.
            skipped_frames += 1
            continue
    return frames, skipped_frames


def _normalize_canvas(
    frames: list[Image.Image],
    resample: Image.Resampling,
    force_even_size: bool,
) -> list[Image.Image]:
    max_w = max(f.width for f in frames)
    max_h = max(f.height for f in frames)
    if force_even_size:
        if max_w % 2 == 1:
            max_w += 1
        if max_h % 2 == 1:
            max_h += 1
    normalized: list[Image.Image] = []

    for frame in frames:
        if frame.width == max_w and frame.height == max_h:
            normalized.append(frame)
            continue
        # Resize smaller/larger frames to a unified canvas size to avoid letterboxing.
        resized = frame.resize((max_w, max_h), resample)
        normalized.append(resized)

    return normalized


def _cleanup_failed_output(output_path: Path) -> None:
    try:
        if output_path.exists() and output_path.is_file():
            output_path.unlink()
    except Exception:
        # Cleanup failure should not mask the original conversion error.
        pass


def _convert_one_archive(
    archive_path: Path,
    output_path: Path,
    anim_format: str,
    duration_ms: int,
    loop: int,
    quality: int,
    webp_method: int,
    video_ffmpeg_threads: int,
    video_webm_crf: int,
    video_webm_cpu_used: int,
    video_mp4_preset: str,
    video_mp4_cq: int,
    overwrite: bool,
) -> ConvertResult:
    fmt = anim_format.lower()
    if fmt == "auto":
        fmt = "webp"
    if fmt not in OUTPUT_FORMATS:
        raise ValueError(f"不支持的输出格式: {anim_format}")

    if output_path.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在: {output_path}")

    frames, skipped_frames = _load_frames_by_internal_order(archive_path)
    if len(frames) < 2:
        raise SkipArchiveError("自动跳过单图压缩包（可用图片帧少于 2，无法生成动图）")

    resize_resample = Image.Resampling.BILINEAR if fmt in {"webm", "mp4"} else Image.Resampling.LANCZOS
    frames = _normalize_canvas(frames, resize_resample, force_even_size=fmt in {"webm", "mp4"})
    first, rest = frames[0], frames[1:]

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
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
        elif fmt in {"webm", "mp4"}:
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg is None:
                raise RuntimeError("未找到 ffmpeg，请先安装并加入 PATH")

            fps = 1000.0 / float(duration_ms)
            width, height = first.size

            if fmt == "webm":
                cmd = [
                    ffmpeg,
                    "-y",
                    "-f",
                    "rawvideo",
                    "-pixel_format",
                    "rgb24",
                    "-video_size",
                    f"{width}x{height}",
                    "-framerate",
                    f"{fps:.6f}",
                    "-i",
                    "-",
                    "-vsync",
                    "0",
                    "-an",
                    "-c:v",
                    "libvpx-vp9",
                    "-pix_fmt",
                    "yuv420p",
                    "-b:v",
                    "0",
                    "-crf",
                    str(video_webm_crf),
                    "-deadline",
                    "realtime",
                    "-cpu-used",
                    str(video_webm_cpu_used),
                    "-row-mt",
                    "1",
                    str(output_path),
                ]
            else:
                # mp4 path is pinned to AV1 NVENC as requested.
                cmd = [
                    ffmpeg,
                    "-y",
                    "-f",
                    "rawvideo",
                    "-pixel_format",
                    "rgb24",
                    "-video_size",
                    f"{width}x{height}",
                    "-framerate",
                    f"{fps:.6f}",
                    "-i",
                    "-",
                    "-vsync",
                    "0",
                    "-an",
                    "-c:v",
                    "av1_nvenc",
                    "-rc",
                    "vbr",
                    "-b:v",
                    "0",
                    "-pix_fmt",
                    "yuv420p",
                    "-preset",
                    video_mp4_preset,
                    "-cq:v",
                    str(video_mp4_cq),
                    str(output_path),
                ]

            if video_ffmpeg_threads > 0:
                cmd[1:1] = ["-threads", str(video_ffmpeg_threads)]

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            stderr_text = ""
            try:
                assert proc.stdin is not None
                proc.stdin.write(first.convert("RGB").tobytes())
                for frame in rest:
                    proc.stdin.write(frame.convert("RGB").tobytes())
                proc.stdin.close()
                stderr_text = (proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else "")
                code = proc.wait()
                if code != 0:
                    raise RuntimeError(f"ffmpeg 编码失败（{fmt}）: {stderr_text[-500:]}")
            except (BrokenPipeError, OSError) as pipe_exc:
                stderr_text = (proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else "")
                proc.wait()
                errno_note = ""
                if isinstance(pipe_exc, OSError) and getattr(pipe_exc, "errno", None) is not None:
                    errno_note = f" errno={pipe_exc.errno}"
                raise RuntimeError(
                    f"ffmpeg 管道中断（{fmt}{errno_note}），通常是编码器/参数不兼容: {stderr_text[-500:]}"
                )
            except Exception:
                proc.kill()
                raise
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

        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise RuntimeError(f"输出文件无效（空文件）: {output_path}")
    except Exception:
        _cleanup_failed_output(output_path)
        raise

    return ConvertResult(
        archive_path=archive_path,
        output_path=output_path,
        frame_count=len(frames),
        skipped_frames=skipped_frames,
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


def _find_common_parent(paths: list[Path]) -> Path:
    """找到所有路径的公共祖先目录。

    对于文件，取其所在目录；然后求所有目录的最长公共前缀。
    """
    if not paths:
        return Path.cwd()
    if len(paths) == 1:
        return paths[0].parent if paths[0].is_file() else paths[0]

    dirs = [p.parent if p.is_file() else p for p in paths]
    common = dirs[0]
    for d in dirs[1:]:
        # 逐级缩短 common 直到 d 是 common 的后代
        while not d.is_relative_to(common) and common != common.parent:
            common = common.parent
        if common == common.parent:
            # 已到根目录
            break
    return common


def _build_output_path(
    archive_path: Path,
    output_root: Path | None,
    target_ext: str,
    template: str,
    prefix: str,
    out_mode: str = "same",
    common_root: Path | None = None,
) -> Path:
    if out_mode == "separate":
        # 公共祖先目录（默认回退到压缩包所在目录）
        root = common_root if common_root is not None else archive_path.parent
        # 输出基础目录 = common_root 的上一级（或指定的 output_root）
        base = output_root if output_root is not None else root.parent
        # 最顶层文件夹名加 prefix
        root_name = root.name or "output"
        prefixed_dir = _sanitize_output_stem(f"{prefix}{root_name}")
        # 压缩包相对于 common_root 的相对路径结构
        rel = archive_path.parent.relative_to(root)
        # 文件名不加 prefix（prefix 已体现在顶层文件夹名上）
        out_stem = _render_output_stem(archive_path, "{stem}", "")
        return base / prefixed_dir / rel / f"{out_stem}{target_ext}"
    # same 模式（默认）：输出到压缩包同目录
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
    out_mode: str | None,
    fmt: str | None,
    duration_ms: int | None,
    loop: int | None,
    quality: int | None,
    webp_method: int | None,
    video_ffmpeg_threads: int | None,
    video_webm_crf: int | None,
    video_webm_cpu_used: int | None,
    video_mp4_preset: str | None,
    video_mp4_cq: int | None,
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
    effective_video_ffmpeg_threads = (
        video_ffmpeg_threads if video_ffmpeg_threads is not None else app_config.video.ffmpeg_threads
    )
    effective_video_webm_crf = video_webm_crf if video_webm_crf is not None else app_config.video.webm_crf
    effective_video_webm_cpu_used = (
        video_webm_cpu_used if video_webm_cpu_used is not None else app_config.video.webm_cpu_used
    )
    effective_video_mp4_preset = video_mp4_preset if video_mp4_preset is not None else app_config.video.mp4_preset
    effective_video_mp4_cq = video_mp4_cq if video_mp4_cq is not None else app_config.video.mp4_cq
    effective_prefix = name_prefix if name_prefix is not None else app_config.naming.prefix
    effective_template = name_template if name_template is not None else app_config.naming.template
    effective_out_mode = (out_mode or app_config.output.out_mode).lower()
    if effective_out_mode not in {"same", "separate"}:
        console.print("[red]out-mode 仅支持: same, separate[/red]")
        raise typer.Exit(2)

    if "{stem}" not in effective_template:
        effective_template = f"{effective_template}{{stem}}"

    fmt = effective_fmt
    if fmt not in {"gif", "webp", "apng", "webm", "mp4", "auto"}:
        console.print("[red]format 仅支持: gif, webp, apng, webm, mp4, auto[/red]")
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
    if effective_video_ffmpeg_threads < 0:
        console.print("[red]video-ffmpeg-threads 必须大于等于 0[/red]")
        raise typer.Exit(2)
    if effective_video_webm_crf < 0 or effective_video_webm_crf > 63:
        console.print("[red]video-webm-crf 必须在 0-63[/red]")
        raise typer.Exit(2)
    if effective_video_webm_cpu_used < 0 or effective_video_webm_cpu_used > 8:
        console.print("[red]video-webm-cpu-used 必须在 0-8[/red]")
        raise typer.Exit(2)
    if effective_video_mp4_preset not in {"p1", "p2", "p3", "p4", "p5", "p6", "p7"}:
        console.print("[red]video-mp4-preset 必须是 p1-p7[/red]")
        raise typer.Exit(2)
    if effective_video_mp4_cq < 0 or effective_video_mp4_cq > 63:
        console.print("[red]video-mp4-cq 必须在 0-63[/red]")
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

    # 提前检测并跳过单图压缩包（不解码图片内容，仅按扩展名统计）
    archives_to_process: list[Path] = []
    pre_skipped = 0
    for archive_path in archives_found:
        img_count = _count_image_entries(archive_path)
        if img_count < 2:
            pre_skipped += 1
            console.print(
                f"[yellow]跳过[/yellow] {escape(str(archive_path))}: "
                f"自动跳过单图压缩包（可用图片帧少于 2，无法生成动图）"
            )
        else:
            archives_to_process.append(archive_path)
    if pre_skipped > 0:
        console.print(f"[yellow]预检跳过单图压缩包: {pre_skipped} 个[/yellow]")
    if not archives_to_process:
        console.print("[yellow]无有效多图压缩包可处理[/yellow]")
        raise typer.Exit(0)

    target_ext = ".webp" if fmt == "auto" else f".{fmt}"
    output_root = Path(out_dir).expanduser().resolve() if out_dir else None

    # 计算 separate 模式需要的公共祖先目录
    common_root: Path | None = None
    if effective_out_mode == "separate":
        common_root = _find_common_parent(archives_to_process)
        if output_root is not None:
            console.print(
                f"[blue]独立输出模式: 公共目录 {escape(str(common_root))} -> "
                f"输出至 {escape(str(output_root))} 下 {effective_prefix}{escape(common_root.name)}/[/blue]"
            )
        else:
            console.print(
                f"[blue]独立输出模式: 公共目录 {escape(str(common_root))} -> "
                f"输出至 {escape(str(common_root.parent))} 下 {effective_prefix}{escape(common_root.name)}/[/blue]"
            )
    workers = _resolve_max_workers(max_workers, app_config.performance.max_workers, len(archives_to_process))
    console.print(f"[blue]并行线程: {workers}[/blue]")
    if fmt in {"webm", "mp4"}:
        video_fps = 1000.0 / float(effective_duration_ms)
        console.print(f"[blue]视频输出帧率: {video_fps:.3f} fps[/blue]")

    ok = 0
    failed = 0
    skipped = 0
    total_frames = 0
    started = time.perf_counter()
    if workers == 1:
        for archive_path in archives_to_process:
            output_path = _build_output_path(archive_path, output_root, target_ext, effective_template, effective_prefix, effective_out_mode, common_root)
            try:
                result = _convert_one_archive(
                    archive_path=archive_path,
                    output_path=output_path,
                    anim_format=fmt,
                    duration_ms=effective_duration_ms,
                    loop=effective_loop,
                    quality=effective_quality,
                    webp_method=effective_webp_method,
                    video_ffmpeg_threads=effective_video_ffmpeg_threads,
                    video_webm_crf=effective_video_webm_crf,
                    video_webm_cpu_used=effective_video_webm_cpu_used,
                    video_mp4_preset=effective_video_mp4_preset,
                    video_mp4_cq=effective_video_mp4_cq,
                    overwrite=overwrite,
                )
                ok += 1
                total_frames += result.frame_count
                console.print(
                    f"[green]完成[/green] {escape(str(result.archive_path))} -> "
                    f"{escape(str(result.output_path))} ({result.frame_count} 帧)"
                )
                if result.skipped_frames > 0:
                    console.print(f"[yellow]跳过损坏帧: {result.skipped_frames}[/yellow]")
            except SkipArchiveError as exc:
                skipped += 1
                console.print(f"[yellow]跳过[/yellow] {escape(str(archive_path))}: {escape(str(exc))}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                console.print(f"[red]失败[/red] {escape(str(archive_path))}: {escape(str(exc))}")
    else:
        future_map = {}
        with ProcessPoolExecutor(max_workers=workers) as executor:
            for archive_path in archives_to_process:
                output_path = _build_output_path(archive_path, output_root, target_ext, effective_template, effective_prefix, effective_out_mode, common_root)
                fut = executor.submit(
                    _convert_one_archive,
                    archive_path,
                    output_path,
                    fmt,
                    effective_duration_ms,
                    effective_loop,
                    effective_quality,
                    effective_webp_method,
                    effective_video_ffmpeg_threads,
                    effective_video_webm_crf,
                    effective_video_webm_cpu_used,
                    effective_video_mp4_preset,
                    effective_video_mp4_cq,
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
                    if result.skipped_frames > 0:
                        console.print(f"[yellow]跳过损坏帧: {result.skipped_frames}[/yellow]")
                except SkipArchiveError as exc:
                    skipped += 1
                    console.print(f"[yellow]跳过[/yellow] {escape(str(archive_path))}: {escape(str(exc))}")
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    console.print(f"[red]失败[/red] {escape(str(archive_path))}: {escape(str(exc))}")

    elapsed = max(0.0001, time.perf_counter() - started)
    fps = total_frames / elapsed
    console.print(f"[cyan]性能: {total_frames} 帧 / {elapsed:.2f}s = {fps:.2f} 帧/s[/cyan]")
    console.print(f"[cyan]处理完成: 成功 {ok}，跳过 {skipped + pre_skipped}，失败 {failed}，总计 {len(archives_found)}[/cyan]")


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
        choices=["gif", "webp", "apng", "webm", "mp4", "auto"],
        default=app_config.output.format,
    )
    is_video_output = fmt in {"webm", "mp4"}
    is_webp_output = fmt in {"webp", "auto"}

    duration_ms = int(Prompt.ask("每帧时长毫秒", default=str(app_config.output.duration_ms)))

    loop: int | None
    quality: int | None
    webp_method: int | None
    video_ffmpeg_threads: int | None
    video_webm_crf: int | None
    video_webm_cpu_used: int | None
    video_mp4_preset: str | None
    video_mp4_cq: int | None

    if is_video_output:
        loop = None
        quality = None
        webp_method = None
        video_ffmpeg_threads = int(
            Prompt.ask("ffmpeg 线程数（0 自动）", default=str(app_config.video.ffmpeg_threads))
        )
        if fmt == "webm":
            video_webm_crf = int(Prompt.ask("webm CRF（0-63，越大越快越小）", default=str(app_config.video.webm_crf)))
            video_webm_cpu_used = int(
                Prompt.ask("webm cpu-used（0-8，越大越快）", default=str(app_config.video.webm_cpu_used))
            )
            video_mp4_preset = None
            video_mp4_cq = None
        else:
            video_mp4_preset = Prompt.ask(
                "mp4 NVENC preset（p1-p7，越小越快）",
                choices=["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
                default=app_config.video.mp4_preset,
            )
            video_mp4_cq = int(Prompt.ask("mp4 CQ（0-63，越小越清晰）", default=str(app_config.video.mp4_cq)))
            video_webm_crf = None
            video_webm_cpu_used = None
    else:
        loop = int(Prompt.ask("循环次数（0 为无限）", default=str(app_config.output.loop)))
        video_ffmpeg_threads = None
        video_webm_crf = None
        video_webm_cpu_used = None
        video_mp4_preset = None
        video_mp4_cq = None
        if is_webp_output:
            quality = int(Prompt.ask("webp 质量（1-100）", default=str(app_config.output.quality)))
            webp_method = int(
                Prompt.ask("webp 编码速度档位（0-6，越低越快）", default=str(app_config.output.webp_method))
            )
        else:
            quality = None
            webp_method = None

    workers = int(Prompt.ask("并行线程数（0 自动）", default=str(app_config.performance.max_workers)))
    name_prefix = Prompt.ask("输出名前缀", default=app_config.naming.prefix)
    name_template = Prompt.ask("命名模板（可用 {prefix} {stem} {archive} {parent}）", default=app_config.naming.template)
    overwrite = Confirm.ask("输出已存在时是否覆盖", default=False)

    out_mode = Prompt.ask("输出模式", choices=["same", "separate"], default="same")
    out_dir_raw = Prompt.ask("输出目录（留空则 same 模式与原压缩包同目录 / separate 模式为压缩包上一级）", default="")
    out_dir = _clean_line_path(out_dir_raw) or None

    _run_make(
        archives=archives,
        list_file=list_file,
        clipboard=use_clipboard,
        recursive=recursive,
        out_dir=out_dir,
        out_mode=out_mode,
        fmt=fmt,
        duration_ms=duration_ms,
        loop=loop,
        quality=quality,
        webp_method=webp_method,
        video_ffmpeg_threads=video_ffmpeg_threads,
        video_webm_crf=video_webm_crf,
        video_webm_cpu_used=video_webm_cpu_used,
        video_mp4_preset=video_mp4_preset,
        video_mp4_cq=video_mp4_cq,
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
    out_mode: str | None = typer.Option(None, "--out-mode", help="输出模式: same(同目录) | separate(上一级创建prefix子文件夹)"),
    config: str | None = typer.Option(None, "--config", help="gifu 配置文件路径"),
    fmt: str | None = typer.Option(None, "--format", help="输出格式: gif|webp|apng|webm|mp4|auto", case_sensitive=False),
    duration_ms: int | None = typer.Option(None, "--duration", help="每帧时长（毫秒），默认读取配置"),
    loop: int | None = typer.Option(None, "--loop", help="循环次数，0 为无限，默认读取配置"),
    quality: int | None = typer.Option(None, "--quality", help="webp 质量（1-100）"),
    webp_method: int | None = typer.Option(None, "--webp-method", help="webp 编码档位 0-6，越低越快"),
    video_ffmpeg_threads: int | None = typer.Option(None, "--video-ffmpeg-threads", help="视频 ffmpeg 线程数，0 自动"),
    video_webm_crf: int | None = typer.Option(None, "--video-webm-crf", help="webm CRF 0-63，越大速度越快"),
    video_webm_cpu_used: int | None = typer.Option(None, "--video-webm-cpu-used", help="webm cpu-used 0-8，越大速度越快"),
    video_mp4_preset: str | None = typer.Option(None, "--video-mp4-preset", help="mp4 av1_nvenc preset p1-p7，越小越快"),
    video_mp4_cq: int | None = typer.Option(None, "--video-mp4-cq", help="mp4 CQ 0-63，越小画质越高"),
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
        out_mode=out_mode,
        config=config,
        fmt=fmt,
        duration_ms=duration_ms,
        loop=loop,
        quality=quality,
        webp_method=webp_method,
        video_ffmpeg_threads=video_ffmpeg_threads,
        video_webm_crf=video_webm_crf,
        video_webm_cpu_used=video_webm_cpu_used,
        video_mp4_preset=video_mp4_preset,
        video_mp4_cq=video_mp4_cq,
        name_prefix=name_prefix,
        name_template=name_template,
        max_workers=max_workers,
        overwrite=overwrite,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
