from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm

from .grouping import plan_groups_for_folder
from .operations import apply_groups, undo_from_log
from .path_input import resolve_group_root
from .scanner import collect_folder_batches, has_images_in_children
from .ui import (
    show_done_panel,
    show_dry_run_panel,
    show_entry_guide,
    show_groups_table,
    show_root_panel,
)

console = Console(highlight=False)
app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="Group variation images by visual similarity within each folder.",
)


@app.callback(invoke_without_command=True)
def entry(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        show_entry_guide(console)
        raise typer.Exit(0)


@app.command("group")
def group_command(
    folder: Optional[str] = typer.Argument(None, help="Root folder to process"),
    clipboard: bool = typer.Option(False, "--clipboard", help="Read folder path from clipboard when folder is omitted"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Traverse sub-folders, default enabled"),
    scan_order: str = typer.Option("smallest-first", "--scan-order", help="Folder scan order", case_sensitive=False),
    threshold: float = typer.Option(0.17, "--threshold", help="Similarity threshold, lower is stricter"),
    min_group_size: int = typer.Option(2, "--min-group-size", help="Minimum files needed for a group"),
    preview_limit: int = typer.Option(5, "--preview-limit", help="Show N filenames per group in preview"),
    apply: bool = typer.Option(False, "--apply", help="Apply changes to filesystem"),
    mode: str = typer.Option("move", "--mode", help="How to place files into group folders", case_sensitive=False),
) -> None:
    scan_order = scan_order.lower()
    if scan_order not in {"smallest-first", "deepest-first", "natural"}:
        console.print("[red]scan-order 仅支持: smallest-first, deepest-first, natural[/red]")
        raise typer.Exit(2)

    mode = mode.lower()
    if mode not in {"move", "copy", "link"}:
        console.print("[red]mode 仅支持: move, copy, link[/red]")
        raise typer.Exit(2)

    root = resolve_group_root(console, folder, clipboard)
    if root is None:
        raise typer.Exit(2)

    show_root_panel(console, root, recursive, scan_order)

    folder_batches = collect_folder_batches(root, recursive=recursive, scan_order=scan_order)
    if not folder_batches and not recursive and has_images_in_children(root):
        if Confirm.ask("当前目录无图片，但子目录有图片，是否启用递归后重试", default=True):
            recursive = True
            show_root_panel(console, root, recursive, scan_order)
            folder_batches = collect_folder_batches(root, recursive=True, scan_order=scan_order)

    if not folder_batches:
        console.print("[yellow]未找到图片文件[/yellow]")
        raise typer.Exit(0)

    groups = []
    with console.status("[bold cyan]正在计算相似分组..."):
        for folder_path, image_paths in folder_batches:
            groups.extend(
                plan_groups_for_folder(
                    folder=folder_path,
                    image_paths=image_paths,
                    threshold=threshold,
                    min_group_size=min_group_size,
                )
            )

    if not groups:
        console.print("[yellow]当前阈值下没有可分组结果[/yellow]")
        raise typer.Exit(0)

    total_files = show_groups_table(console, groups, root, preview_limit)
    moved_files, created_groups, undo_log = apply_groups(root=root, groups=groups, mode=mode, apply=apply)

    if apply:
        show_done_panel(console, created_groups, moved_files, mode, undo_log)
    else:
        show_dry_run_panel(console, len(folder_batches), len(groups), total_files)


@app.command("undo")
def undo_command(
    log_file: str = typer.Argument(..., help="Path to .simiu-undo-*.json"),
    clean_empty_dirs: bool = typer.Option(False, "--clean-empty-dirs", help="Try removing now-empty group directories"),
) -> None:
    path = Path(log_file).expanduser().resolve()
    if not path.exists():
        console.print(f"[red]日志文件不存在: {escape(str(path))}[/red]")
        raise typer.Exit(2)

    try:
        reverted = undo_from_log(path, clean_empty_dirs)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)

    console.print(f"[green]Undo 完成，已回滚操作数: {reverted}[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
