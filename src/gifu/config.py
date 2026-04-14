from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except Exception:  # noqa: BLE001
    tomllib = None


@dataclass
class OutputConfig:
    format: str = "webp"
    quality: int = 85
    webp_method: int = 4
    duration_ms: int = 120
    loop: int = 0


@dataclass
class NamingConfig:
    prefix: str = "[#dyna]"
    template: str = "{prefix}{stem}"


@dataclass
class PerformanceConfig:
    max_workers: int = 0


@dataclass
class AppConfig:
    output: OutputConfig
    naming: NamingConfig
    performance: PerformanceConfig
    source_path: Path | None


def _candidate_config_paths(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]

    package_repo = Path(__file__).resolve().parents[2]
    package_dir = Path(__file__).resolve().parent

    candidates = [
        Path.cwd() / "gifu.toml",
        Path.cwd() / ".gifu.toml",
        package_repo / "gifu.toml",
        package_dir / "gifu.toml",
    ]

    result: list[Path] = []
    seen: set[Path] = set()
    for p in candidates:
        rp = p.resolve() if p.exists() else p
        if rp not in seen:
            result.append(p)
            seen.add(rp)
    return result


def _sanitize_format(value: str) -> str:
    fmt = value.strip().lower()
    if fmt not in {"gif", "webp", "apng", "auto"}:
        return "webp"
    return fmt


def _sanitize_quality(value: int) -> int:
    if value < 1:
        return 1
    if value > 100:
        return 100
    return value


def _sanitize_webp_method(value: int) -> int:
    if value < 0:
        return 0
    if value > 6:
        return 6
    return value


def _sanitize_duration_ms(value: int) -> int:
    return value if value > 0 else 120


def _sanitize_loop(value: int) -> int:
    return value if value >= 0 else 0


def _sanitize_prefix(value: str) -> str:
    prefix = value.strip()
    return prefix if prefix else "[#dyna]"


def _sanitize_template(value: str) -> str:
    template = value.strip()
    if not template:
        return "{prefix}{stem}"
    if "{stem}" not in template:
        template = f"{template}{{stem}}"
    return template


def _sanitize_max_workers(value: int) -> int:
    return value if value >= 0 else 0


def load_config(config_path: str | None = None) -> AppConfig:
    chosen: Path | None = None
    data: dict = {}

    for path in _candidate_config_paths(config_path):
        if path.exists() and path.is_file():
            chosen = path.resolve()
            if tomllib is None:
                break
            with chosen.open("rb") as f:
                loaded = tomllib.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            break

    output_data = data.get("output", {}) if isinstance(data, dict) else {}
    naming_data = data.get("naming", {}) if isinstance(data, dict) else {}
    perf_data = data.get("performance", {}) if isinstance(data, dict) else {}

    raw_format = output_data.get("format", "webp") if isinstance(output_data, dict) else "webp"
    raw_quality = output_data.get("quality", 85) if isinstance(output_data, dict) else 85
    raw_webp_method = output_data.get("webp_method", 4) if isinstance(output_data, dict) else 4
    raw_duration_ms = output_data.get("duration_ms", 120) if isinstance(output_data, dict) else 120
    raw_loop = output_data.get("loop", 0) if isinstance(output_data, dict) else 0

    raw_prefix = naming_data.get("prefix", "[#dyna]") if isinstance(naming_data, dict) else "[#dyna]"
    raw_template = naming_data.get("template", "{prefix}{stem}") if isinstance(naming_data, dict) else "{prefix}{stem}"
    raw_workers = perf_data.get("max_workers", 0) if isinstance(perf_data, dict) else 0

    try:
        quality = int(raw_quality)
    except (TypeError, ValueError):
        quality = 85

    try:
        webp_method = int(raw_webp_method)
    except (TypeError, ValueError):
        webp_method = 4

    try:
        duration_ms = int(raw_duration_ms)
    except (TypeError, ValueError):
        duration_ms = 120

    try:
        loop = int(raw_loop)
    except (TypeError, ValueError):
        loop = 0

    try:
        max_workers = int(raw_workers)
    except (TypeError, ValueError):
        max_workers = 0

    return AppConfig(
        output=OutputConfig(
            format=_sanitize_format(str(raw_format)),
            quality=_sanitize_quality(quality),
            webp_method=_sanitize_webp_method(webp_method),
            duration_ms=_sanitize_duration_ms(duration_ms),
            loop=_sanitize_loop(loop),
        ),
        naming=NamingConfig(
            prefix=_sanitize_prefix(str(raw_prefix)),
            template=_sanitize_template(str(raw_template)),
        ),
        performance=PerformanceConfig(max_workers=_sanitize_max_workers(max_workers)),
        source_path=chosen,
    )
