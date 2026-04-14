from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass
class GroupConfig:
    name_prefix: str = "simiu_set"


@dataclass
class AppConfig:
    group: GroupConfig
    source_path: Path | None


def _sanitize_prefix(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return "simiu_set"

    # Keep folder-safe and readable prefix.
    banned = '<>:"/\\|?*'
    for ch in banned:
        candidate = candidate.replace(ch, "_")
    candidate = candidate.strip().strip(".")
    return candidate or "simiu_set"


def _candidate_config_paths(root: Path, explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]

    candidates = [
        Path.cwd() / "simiu.toml",
        root / "simiu.toml",
        root / ".simiu.toml",
    ]

    result: list[Path] = []
    seen: set[Path] = set()
    for p in candidates:
        rp = p.resolve() if p.exists() else p
        if rp not in seen:
            result.append(p)
            seen.add(rp)
    return result


def load_config(root: Path, config_path: str | None = None) -> AppConfig:
    chosen: Path | None = None
    data: dict = {}

    for path in _candidate_config_paths(root, config_path):
        if path.exists() and path.is_file():
            chosen = path.resolve()
            with chosen.open("rb") as f:
                loaded = tomllib.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            break

    group_data = data.get("group", {}) if isinstance(data, dict) else {}
    raw_prefix = group_data.get("name_prefix", "simiu_set") if isinstance(group_data, dict) else "simiu_set"
    prefix = _sanitize_prefix(str(raw_prefix))

    return AppConfig(group=GroupConfig(name_prefix=prefix), source_path=chosen)
