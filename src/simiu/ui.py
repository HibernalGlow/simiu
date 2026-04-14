from __future__ import annotations

from pathlib import Path
from typing import Sequence

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import PlannedGroup


def show_path_input_panel(console: Console) -> None:
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


def show_root_panel(console: Console, root: Path, recursive: bool, scan_order: str) -> None:
    header = Text(f"目标目录: {root}\n递归遍历: {'是' if recursive else '否'}\n扫描顺序: {scan_order}")
    console.print(Panel.fit(header, title="simiu group", border_style="cyan"))


def show_groups_table(
    console: Console,
    groups: Sequence[PlannedGroup],
    root: Path,
    preview_limit: int,
) -> int:
    table = Table(title="分组预览", box=box.SIMPLE_HEAVY)
    table.add_column("序号", style="cyan", justify="right")
    table.add_column("目录", style="magenta")
    table.add_column("分组名", style="green")
    table.add_column("文件数", style="yellow", justify="right")
    table.add_column("示例文件", style="white")

    total_files = 0
    for idx, g in enumerate(groups, start=1):
        total_files += len(g.files)
        sample = ", ".join(f.name for f in g.files[:preview_limit])
        more = "" if len(g.files) <= preview_limit else f" ... +{len(g.files) - preview_limit}"
        try:
            rel = "." if g.parent_dir == root else str(g.parent_dir.relative_to(root))
        except ValueError:
            rel = str(g.parent_dir)
        table.add_row(f"{idx:03d}", escape(rel), escape(g.name), str(len(g.files)), escape(f"{sample}{more}"))

    console.print(table)
    return total_files


def show_done_panel(console: Console, created_groups: int, moved_files: int, mode: str, undo_log: Path | None) -> None:
    summary = (
        f"已创建分组目录: {created_groups}\n"
        f"已处理文件: {moved_files}\n"
        f"执行模式: {mode}"
    )
    console.print(Panel.fit(summary, title="执行完成", border_style="green"))
    if undo_log is not None:
        console.print(f"[green]回滚日志: {escape(str(undo_log))}[/green]")


def show_dry_run_panel(console: Console, folder_count: int, group_count: int, total_files: int) -> None:
    summary = (
        f"扫描目录数: {folder_count}\n"
        f"识别分组数: {group_count}\n"
        f"预计处理文件: {total_files}"
    )
    console.print(Panel.fit(summary, title="Dry Run", border_style="blue"))
    console.print("[cyan]提示: 添加 --apply 执行实际落盘[/cyan]")


def show_intelligent_suggestions(
    console: Console,
    threshold: float,
    folder_count: int,
    group_count: int,
    total_files: int,
    skipped_all_in_one: int,
    max_workers: int,
) -> None:
    if group_count <= 0:
        return

    avg_group_size = total_files / float(group_count)
    suggestions: list[str] = []

    if skipped_all_in_one > 0:
        tighter = max(0.05, threshold - 0.03)
        suggestions.append(
            f"检测到 {skipped_all_in_one} 个目录出现全量单组，建议尝试更严格阈值 --threshold {tighter:.2f}"
        )

    if folder_count > 0 and group_count > folder_count * 2.2:
        looser = min(1.0, threshold + 0.02)
        suggestions.append(f"分组偏碎，建议尝试更宽松阈值 --threshold {looser:.2f}")
    elif avg_group_size >= 7:
        tighter = max(0.05, threshold - 0.02)
        suggestions.append(f"平均组较大，建议尝试更严格阈值 --threshold {tighter:.2f}")

    if total_files >= 600 and max_workers == 0:
        suggestions.append("大批量图片可在配置中设置 [performance].max_workers = 8 或 12 提升速度")

    if not suggestions:
        suggestions.append("当前阈值表现稳定，可直接 apply")

    text = "\n".join(f"- {s}" for s in suggestions)
    console.print(Panel.fit(text, title="智能建议", border_style="magenta"))


def show_entry_guide(console: Console) -> None:
    guide = (
        "直接输入 simiu 不带子命令时，可用以下入口:\n"
        "1) simiu group <目录>\n"
        "2) simiu group --clipboard\n"
        "3) simiu undo <回滚日志>"
    )
    console.print(Panel.fit(guide, title="simiu 引导", border_style="magenta"))
