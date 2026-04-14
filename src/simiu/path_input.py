from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pyperclip
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.table import Table


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


def select_clipboard_directory(console: Console, paths: Sequence[Path]) -> Path | None:
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


def prompt_directory_interactive(console: Console) -> Path | None:
    from .ui import show_path_input_panel

    show_path_input_panel(console)
    clipboard_paths = parse_clipboard_directories()
    selected = select_clipboard_directory(console, clipboard_paths)
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


def resolve_group_root(console: Console, folder: str | None, clipboard: bool) -> Path | None:
    if folder:
        p = Path(clean_input_path(folder)).expanduser()
        if not p.exists() or not p.is_dir():
            console.print(f"[red]路径不存在或不是目录: {escape(str(p))}[/red]")
            return None
        return p.resolve()

    if clipboard:
        paths = parse_clipboard_directories()
        selected = select_clipboard_directory(console, paths)
        if selected is None:
            console.print("[red]剪贴板中没有可用目录[/red]")
        return selected

    return prompt_directory_interactive(console)
