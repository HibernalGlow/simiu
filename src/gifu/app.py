from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable
import tarfile
import zipfile

import typer
from PIL import Image
from rich.console import Console
from rich.markup import escape

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
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Batch convert archives to animated gif/webp/apng by internal file order.",
)


@dataclass(frozen=True)
class ConvertResult:
    archive_path: Path
    output_path: Path
    frame_count: int


def _clean_line_path(line: str) -> str:
    return line.strip().strip('"').strip("'").strip()


def _is_archive_file(path: Path) -> bool:
    low = path.name.lower()
    return any(low.endswith(ext) for ext in ARCHIVE_EXTS)


def _parse_list_file(path: Path) -> list[Path]:
    if not path.exists() or not path.is_file():
        raise ValueError(f"路径清单文件不存在: {path}")
    result: list[Path] = []
    with path.open("r", encoding="utf-8") as fp:
        for raw in fp:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            cleaned = _clean_line_path(line)
            if cleaned:
                result.append(Path(cleaned))
    return result


def _parse_clipboard_paths() -> list[Path]:
    if pyperclip is None:
        return []

    try:
        text = pyperclip.paste().strip()
    except Exception:
        return []

    if not text:
        return []

    result: list[Path] = []
    for line in text.splitlines():
        cleaned = _clean_line_path(line)
        if cleaned:
            result.append(Path(cleaned))
    return result


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
            method=6,
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


@app.command("make")
def make_command(
    archives: list[str] = typer.Argument(None, help="压缩包路径，支持多个；也可传目录"),
    list_file: str | None = typer.Option(None, "--list-file", help="路径清单文件（每行一个路径）"),
    clipboard: bool = typer.Option(False, "--clipboard", help="从剪贴板读取路径列表（每行一个）"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="输入目录时是否递归查找压缩包"),
    out_dir: str | None = typer.Option(None, "--out-dir", help="输出目录，不传则输出到各压缩包同目录"),
    fmt: str = typer.Option("webp", "--format", help="输出格式: gif|webp|apng|auto", case_sensitive=False),
    duration_ms: int = typer.Option(120, "--duration", help="每帧时长（毫秒）"),
    loop: int = typer.Option(0, "--loop", help="循环次数，0 为无限"),
    quality: int = typer.Option(85, "--quality", help="webp 质量（1-100）"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已存在的输出文件"),
) -> None:
    fmt = fmt.lower()
    if fmt not in {"gif", "webp", "apng", "auto"}:
        console.print("[red]format 仅支持: gif, webp, apng, auto[/red]")
        raise typer.Exit(2)
    if duration_ms <= 0:
        console.print("[red]duration 必须大于 0[/red]")
        raise typer.Exit(2)
    if quality < 1 or quality > 100:
        console.print("[red]quality 必须在 1-100[/red]")
        raise typer.Exit(2)

    raw_inputs: list[Path] = [Path(p) for p in archives]

    if list_file:
        try:
            raw_inputs.extend(_parse_list_file(Path(list_file).expanduser().resolve()))
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(2)

    if clipboard:
        if pyperclip is None:
            console.print("[yellow]未安装 pyperclip，已忽略 --clipboard[/yellow]")
        raw_inputs.extend(_parse_clipboard_paths())

    if not raw_inputs:
        console.print("[red]请至少提供一个压缩包路径（参数/--list-file/--clipboard）[/red]")
        raise typer.Exit(2)

    archives_found = _collect_archives(raw_inputs, recursive=recursive)
    if not archives_found:
        console.print("[yellow]未找到可处理压缩包（支持 zip/cbz/tar/tgz/tar.gz/tbz2/txz）[/yellow]")
        raise typer.Exit(0)

    target_ext = ".webp" if fmt == "auto" else f".{fmt}"
    output_root = Path(out_dir).expanduser().resolve() if out_dir else None

    ok = 0
    failed = 0
    for archive_path in archives_found:
        output_path = (
            output_root / f"{archive_path.stem}{target_ext}"
            if output_root is not None
            else archive_path.with_suffix(target_ext)
        )
        try:
            result = _convert_one_archive(
                archive_path=archive_path,
                output_path=output_path,
                anim_format=fmt,
                duration_ms=duration_ms,
                loop=loop,
                quality=quality,
                overwrite=overwrite,
            )
            ok += 1
            console.print(
                f"[green]完成[/green] {escape(str(result.archive_path))} -> "
                f"{escape(str(result.output_path))} ({result.frame_count} 帧)"
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            console.print(f"[red]失败[/red] {escape(str(archive_path))}: {escape(str(exc))}")

    console.print(f"[cyan]处理完成: 成功 {ok}，失败 {failed}，总计 {len(archives_found)}[/cyan]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
