"""Tests for gifu --out-mode (same / separate) functionality.

核心逻辑：
  separate 模式只对公共祖先目录（最顶层）加 prefix，
  内部保持与原目录完全相同的结构，便于剪切覆盖回去。

  例: D:\\manga\\vol01\\ch1.cbz  (common_root = D:\\manga)
      -> D:\\[#dyna]manga\\vol01\\ch1.webp
      内部结构 vol01\\ch1.webp 与原 manga\\vol01\\ch1.cbz 一致。
"""
from __future__ import annotations

from pathlib import Path
import sys
import zipfile
from io import BytesIO

from PIL import Image

# Ensure src-layout imports work in local test runs.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gifu.app import (
    _build_output_path,
    _convert_one_archive,
    _find_common_parent,
    _sanitize_output_stem,
)  # noqa: E402
from gifu.config import (
    OutputConfig,
    _sanitize_out_mode,
)  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_sample_zip(path: Path, frame_count: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        for idx in range(frame_count):
            color = ((idx * 17) % 255, (idx * 31) % 255, (idx * 53) % 255)
            img = Image.new("RGB", (64, 64), color)
            buf = BytesIO()
            img.save(buf, format="PNG")
            zf.writestr(f"frame_{idx:04d}.png", buf.getvalue())


# ===========================================================================
# _find_common_parent
# ===========================================================================

class TestFindCommonParent:
    """Test _find_common_parent: 找到所有路径的公共祖先目录。"""

    def test_single_file(self) -> None:
        """单个文件 -> 其所在目录。"""
        archive = Path(r"D:\manga\vol01\ch1.cbz")
        assert _find_common_parent([archive]) == Path(r"D:\manga\vol01")

    def test_two_files_same_dir(self) -> None:
        """同目录下两个文件 -> 该目录。"""
        a1 = Path(r"D:\manga\vol01\ch1.cbz")
        a2 = Path(r"D:\manga\vol01\ch2.cbz")
        assert _find_common_parent([a1, a2]) == Path(r"D:\manga\vol01")

    def test_two_files_sibling_dirs(self) -> None:
        """兄弟目录下文件 -> 父目录。"""
        a1 = Path(r"D:\manga\vol01\ch1.cbz")
        a2 = Path(r"D:\manga\vol02\ch1.cbz")
        assert _find_common_parent([a1, a2]) == Path(r"D:\manga")

    def test_deeply_nested_common(self) -> None:
        """深层嵌套 -> 最近公共祖先。"""
        a1 = Path(r"D:\manga\shonen\vol01\ch1.cbz")
        a2 = Path(r"D:\manga\shoujo\vol01\ch1.cbz")
        assert _find_common_parent([a1, a2]) == Path(r"D:\manga")

    def test_three_files_shared_grandparent(self) -> None:
        """三个文件 -> 共同祖父目录。"""
        a1 = Path(r"D:\pics\photos\2024\img1.cbz")
        a2 = Path(r"D:\pics\photos\2025\img2.cbz")
        a3 = Path(r"D:\pics\wallpapers\img3.cbz")
        assert _find_common_parent([a1, a2, a3]) == Path(r"D:\pics")


# ===========================================================================
# _build_output_path unit tests
# ===========================================================================

class TestBuildOutputPath:
    """Test _build_output_path with same and separate modes."""

    # ---- same mode (default) ----

    def test_same_no_out_dir(self) -> None:
        """same 模式、无 --out-dir: 输出到压缩包同目录，文件名加 prefix。"""
        archive = Path(r"D:\manga\vol01\chapter1.cbz")
        result = _build_output_path(
            archive_path=archive,
            output_root=None,
            target_ext=".webp",
            template="{prefix}{stem}",
            prefix="[#dyna]",
            out_mode="same",
        )
        assert result == Path(r"D:\manga\vol01\[#dyna]chapter1.webp")

    def test_same_with_out_dir(self) -> None:
        """same 模式、指定 --out-dir: 所有文件平铺到指定目录。"""
        archive = Path(r"D:\manga\vol01\chapter1.cbz")
        result = _build_output_path(
            archive_path=archive,
            output_root=Path(r"E:\output"),
            target_ext=".webp",
            template="{prefix}{stem}",
            prefix="[#dyna]",
            out_mode="same",
        )
        assert result == Path(r"E:\output\[#dyna]chapter1.webp")

    # ---- separate mode ----

    def test_separate_single_archive(self) -> None:
        """separate 模式: 单个压缩包，common_root 为其父目录。

        archive:  D:\\manga\\vol01\\chapter1.cbz
        common:   D:\\manga\\vol01
        输出:     D:\\[#dyna]vol01\\chapter1.webp
        """
        archive = Path(r"D:\manga\vol01\chapter1.cbz")
        common = Path(r"D:\manga\vol01")
        result = _build_output_path(
            archive_path=archive,
            output_root=None,
            target_ext=".webp",
            template="{prefix}{stem}",
            prefix="[#dyna]",
            out_mode="separate",
            common_root=common,
        )
        assert result == Path(r"D:\manga\[#dyna]vol01\chapter1.webp")

    def test_separate_multiple_archives_shared_parent(self) -> None:
        """separate 模式: 多个压缩包共享公共祖先，只对顶层加 prefix。

        archives: D:\\manga\\vol01\\ch1.cbz, D:\\manga\\vol02\\ch1.cbz
        common:   D:\\manga
        输出:     D:\\[#dyna]manga\\vol01\\ch1.webp
                  D:\\[#dyna]manga\\vol02\\ch1.webp
        """
        archive1 = Path(r"D:\manga\vol01\ch1.cbz")
        archive2 = Path(r"D:\manga\vol02\ch1.cbz")
        common = Path(r"D:\manga")

        r1 = _build_output_path(archive1, None, ".webp", "{prefix}{stem}", "[#dyna]", "separate", common)
        r2 = _build_output_path(archive2, None, ".webp", "{prefix}{stem}", "[#dyna]", "separate", common)

        assert r1 == Path(r"D:\[#dyna]manga\vol01\ch1.webp")
        assert r2 == Path(r"D:\[#dyna]manga\vol02\ch1.webp")

    def test_separate_internal_structure_preserved(self) -> None:
        """separate 模式: 内部目录结构与原目录完全一致（便于剪切覆盖）。

        archive:  D:\\manga\\shonen\\vol01\\ch1.cbz
        common:   D:\\manga
        输出:     D:\\[#dyna]manga\\shonen\\vol01\\ch1.webp
                  ^^^^^^^^^^^^ 仅顶层加 prefix
                               ^^^^^^^^^^^^^^^^ 内部结构不变
        """
        archive = Path(r"D:\manga\shonen\vol01\ch1.cbz")
        common = Path(r"D:\manga")
        result = _build_output_path(
            archive_path=archive,
            output_root=None,
            target_ext=".webp",
            template="{prefix}{stem}",
            prefix="[#dyna]",
            out_mode="separate",
            common_root=common,
        )
        assert result == Path(r"D:\[#dyna]manga\shonen\vol01\ch1.webp")
        # 内部路径 shonen\vol01 与原结构一致
        assert result.relative_to(Path(r"D:\[#dyna]manga")) == Path(r"shonen\vol01\ch1.webp")

    def test_separate_with_out_dir(self) -> None:
        """separate 模式 + --out-dir: 在指定目录下创建 prefix 顶层文件夹。

        archive:  D:\\manga\\vol01\\ch1.cbz
        common:   D:\\manga
        out-dir:  E:\\output
        输出:     E:\\output\\[#dyna]manga\\vol01\\ch1.webp
        """
        archive = Path(r"D:\manga\vol01\ch1.cbz")
        common = Path(r"D:\manga")
        result = _build_output_path(
            archive_path=archive,
            output_root=Path(r"E:\output"),
            target_ext=".webp",
            template="{prefix}{stem}",
            prefix="[#dyna]",
            out_mode="separate",
            common_root=common,
        )
        assert result == Path(r"E:\output\[#dyna]manga\vol01\ch1.webp")

    def test_separate_filename_no_prefix(self) -> None:
        """separate 模式: 文件名不加 prefix（prefix 仅在顶层文件夹名上）。"""
        archive = Path(r"D:\manga\vol01\chapter1.cbz")
        common = Path(r"D:\manga")
        result = _build_output_path(
            archive_path=archive,
            output_root=None,
            target_ext=".webp",
            template="{prefix}{stem}",
            prefix="[#dyna]",
            out_mode="separate",
            common_root=common,
        )
        assert result.name == "chapter1.webp"
        # 顶层文件夹名含 prefix
        top_dir = result.parts[result.parts.index("[#dyna]manga")]
        assert top_dir == "[#dyna]manga"

    def test_separate_custom_prefix(self) -> None:
        """separate 模式: 自定义 prefix。"""
        archive = Path(r"D:\pics\photos\img.cbz")
        common = Path(r"D:\pics")
        result = _build_output_path(
            archive_path=archive,
            output_root=None,
            target_ext=".webp",
            template="{prefix}{stem}",
            prefix="[anim]",
            out_mode="separate",
            common_root=common,
        )
        assert result == Path(r"D:\[anim]pics\photos\img.webp")

    def test_separate_gif_format(self) -> None:
        """separate 模式: gif 格式扩展名。"""
        archive = Path(r"D:\manga\vol01\chapter1.cbz")
        common = Path(r"D:\manga")
        result = _build_output_path(
            archive_path=archive,
            output_root=None,
            target_ext=".gif",
            template="{prefix}{stem}",
            prefix="[#dyna]",
            out_mode="separate",
            common_root=common,
        )
        assert result == Path(r"D:\[#dyna]manga\vol01\chapter1.gif")


# ===========================================================================
# Config: out_mode
# ===========================================================================

class TestOutModeConfig:
    """Test out_mode config parsing and sanitization."""

    def test_sanitize_same(self) -> None:
        assert _sanitize_out_mode("same") == "same"

    def test_sanitize_separate(self) -> None:
        assert _sanitize_out_mode("separate") == "separate"

    def test_sanitize_case_insensitive(self) -> None:
        assert _sanitize_out_mode("SEPARATE") == "separate"
        assert _sanitize_out_mode("Same") == "same"

    def test_sanitize_invalid_fallback(self) -> None:
        assert _sanitize_out_mode("other") == "same"
        assert _sanitize_out_mode("") == "same"

    def test_sanitize_whitespace(self) -> None:
        assert _sanitize_out_mode("  separate  ") == "separate"

    def test_output_config_default(self) -> None:
        cfg = OutputConfig()
        assert cfg.out_mode == "same"


# ===========================================================================
# Integration: _convert_one_archive with separate mode
# ===========================================================================

class TestConvertSeparateMode:
    """Integration test: actual file output with separate mode."""

    def test_separate_creates_top_level_prefixed_folder(self, tmp_path: Path) -> None:
        """separate 模式: 在上一级创建 prefix+顶层目录名 文件夹，内部结构不变。

        目录: tmp_path / manga / vol01 / chapter1.cbz
        common: tmp_path / manga
        输出: tmp_path / [#dyna]manga / vol01 / chapter1.webp
        """
        manga_dir = tmp_path / "manga" / "vol01"
        archive = manga_dir / "chapter1.cbz"
        _build_sample_zip(archive)

        common = tmp_path / "manga"
        output_path = _build_output_path(
            archive_path=archive,
            output_root=None,
            target_ext=".webp",
            template="{prefix}{stem}",
            prefix="[#dyna]",
            out_mode="separate",
            common_root=common,
        )
        assert output_path == tmp_path / "[#dyna]manga" / "vol01" / "chapter1.webp"

        result = _convert_one_archive(
            archive_path=archive,
            output_path=output_path,
            anim_format="webp",
            duration_ms=120,
            loop=0,
            quality=85,
            webp_method=2,
            video_ffmpeg_threads=0,
            video_webm_crf=34,
            video_webm_cpu_used=6,
            video_mp4_preset="p3",
            video_mp4_cq=32,
            overwrite=True,
        )
        assert output_path.exists()
        assert result.frame_count == 3
        # 内部结构 vol01/chapter1.webp 与原 manga/vol01/chapter1.cbz 一致
        rel = result.output_path.relative_to(tmp_path / "[#dyna]manga")
        assert rel == Path("vol01") / "chapter1.webp"

    def test_separate_with_out_dir(self, tmp_path: Path) -> None:
        """separate + --out-dir: 在指定目录下创建 prefix 顶层文件夹。"""
        manga_dir = tmp_path / "manga" / "vol01"
        archive = manga_dir / "chapter1.cbz"
        _build_sample_zip(archive)

        dest = tmp_path / "dest"
        common = tmp_path / "manga"

        output_path = _build_output_path(
            archive_path=archive,
            output_root=dest,
            target_ext=".webp",
            template="{prefix}{stem}",
            prefix="[#dyna]",
            out_mode="separate",
            common_root=common,
        )
        assert output_path == dest / "[#dyna]manga" / "vol01" / "chapter1.webp"

        result = _convert_one_archive(
            archive_path=archive,
            output_path=output_path,
            anim_format="webp",
            duration_ms=120,
            loop=0,
            quality=85,
            webp_method=2,
            video_ffmpeg_threads=0,
            video_webm_crf=34,
            video_webm_cpu_used=6,
            video_mp4_preset="p3",
            video_mp4_cq=32,
            overwrite=True,
        )
        assert output_path.exists()
        # 内部结构 vol01/chapter1.webp 不变
        rel = result.output_path.relative_to(dest / "[#dyna]manga")
        assert rel == Path("vol01") / "chapter1.webp"

    def test_separate_multiple_archives_shared_common(self, tmp_path: Path) -> None:
        """separate 模式: 多个压缩包在同一公共目录下，输出到同一 prefix 文件夹。"""
        vol01 = tmp_path / "manga" / "vol01"
        vol02 = tmp_path / "manga" / "vol02"
        a1 = vol01 / "ch1.cbz"
        a2 = vol02 / "ch1.cbz"
        _build_sample_zip(a1)
        _build_sample_zip(a2)

        common = tmp_path / "manga"
        out1 = _build_output_path(a1, None, ".webp", "{prefix}{stem}", "[#dyna]", "separate", common)
        out2 = _build_output_path(a2, None, ".webp", "{prefix}{stem}", "[#dyna]", "separate", common)

        # 都在 [#dyna]manga 下
        assert out1 == tmp_path / "[#dyna]manga" / "vol01" / "ch1.webp"
        assert out2 == tmp_path / "[#dyna]manga" / "vol02" / "ch1.webp"

        for archive, output in [(a1, out1), (a2, out2)]:
            _convert_one_archive(
                archive_path=archive,
                output_path=output,
                anim_format="webp",
                duration_ms=120,
                loop=0,
                quality=85,
                webp_method=2,
                video_ffmpeg_threads=0,
                video_webm_crf=34,
                video_webm_cpu_used=6,
                video_mp4_preset="p3",
                video_mp4_cq=32,
                overwrite=True,
            )
        assert out1.exists()
        assert out2.exists()

    def test_same_mode_no_subfolder(self, tmp_path: Path) -> None:
        """same 模式: 不创建子文件夹，输出到压缩包同目录。"""
        vol_dir = tmp_path / "vol01"
        archive = vol_dir / "chapter1.cbz"
        _build_sample_zip(archive)

        output_path = _build_output_path(
            archive_path=archive,
            output_root=None,
            target_ext=".webp",
            template="{prefix}{stem}",
            prefix="[#dyna]",
            out_mode="same",
        )
        assert output_path == vol_dir / "[#dyna]chapter1.webp"

        result = _convert_one_archive(
            archive_path=archive,
            output_path=output_path,
            anim_format="webp",
            duration_ms=120,
            loop=0,
            quality=85,
            webp_method=2,
            video_ffmpeg_threads=0,
            video_webm_crf=34,
            video_webm_cpu_used=6,
            video_mp4_preset="p3",
            video_mp4_cq=32,
            overwrite=True,
        )
        assert output_path.exists()
        assert result.output_path.parent == vol_dir
