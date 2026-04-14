from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
import time
import zipfile

from PIL import Image

# Ensure src-layout imports work in local test runs.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gifu.app import _convert_one_archive  # noqa: E402


def _build_sample_zip(path: Path, frame_count: int = 40) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        for idx in range(frame_count):
            # Deterministic synthetic frame to keep benchmark stable.
            color = ((idx * 17) % 255, (idx * 31) % 255, (idx * 53) % 255)
            img = Image.new("RGB", (320, 320), color)
            buf = BytesIO()
            img.save(buf, format="PNG")
            zf.writestr(f"frame_{idx:04d}.png", buf.getvalue())


def test_gifu_webp_fps_at_least_5(tmp_path: Path) -> None:
    archive = tmp_path / "sample.zip"
    output = tmp_path / "out.webp"
    _build_sample_zip(archive, frame_count=40)

    started = time.perf_counter()
    result = _convert_one_archive(
        archive_path=archive,
        output_path=output,
        anim_format="webp",
        duration_ms=120,
        loop=0,
        quality=85,
        webp_method=2,
        overwrite=True,
    )
    elapsed = max(0.0001, time.perf_counter() - started)
    fps = result.frame_count / elapsed

    assert output.exists(), "输出文件未生成"
    assert fps >= 5.0, f"性能未达标: {fps:.2f} 帧/s (< 5.0 帧/s)"
