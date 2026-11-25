"""Photographer culling helpers: zero-VLM focus/exposure heuristics & Original+RAW export."""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BLUR_SHARPNESS_THRESHOLD = 18.0
OVEREXPOSED_RATIO_THRESHOLD = 0.18
UNDEREXPOSED_RATIO_THRESHOLD = 0.35


def classify_quality_flags(
    sharpness: float, overexposed_ratio: float, underexposed_ratio: float
) -> list[str]:
    """Derive human-readable Chinese culling flags from focus & exposure metrics."""
    flags: list[str] = []
    if sharpness < BLUR_SHARPNESS_THRESHOLD:
        flags.append("疑似失焦")
    if overexposed_ratio > OVEREXPOSED_RATIO_THRESHOLD:
        flags.append("疑似过曝")
    if underexposed_ratio > UNDEREXPOSED_RATIO_THRESHOLD:
        flags.append("疑似欠曝")
    return flags


def analyze_photo_quality(image_path: str) -> dict[str, Any] | None:
    """Compute Laplacian focus sharpness and highlight/shadow clipping via Pillow.

    Zero VLM calls, fast enough for interactive side-by-side loupe culling.
    """
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        from PIL import Image, ImageFilter, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(image_path) as raw:
            img = ImageOps.exif_transpose(raw).convert("L")
            img.thumbnail((512, 512), Image.Resampling.BILINEAR)
            pixels = list(img.getdata())
            total = max(1, len(pixels))
            over = sum(1 for p in pixels if p >= 250) / total
            under = sum(1 for p in pixels if p <= 12) / total

            # Discrete 3x3 Laplacian kernel for edge sharpness energy
            lap_kernel = ImageFilter.Kernel(
                (3, 3),
                (0, -1, 0, -1, 4, -1, 0, -1, 0),
                scale=1,
                offset=128,
            )
            lap = img.filter(lap_kernel)
            lap_vals = [v - 128 for v in lap.getdata()]
            mean = sum(lap_vals) / total
            variance = sum((v - mean) ** 2 for v in lap_vals) / total
            # Normalize to an intuitive 0..100 sharpness scale
            sharpness = round(min(100.0, (variance ** 0.5) * 3.2), 1)
    except Exception:
        logger.debug("quality analysis failed for %s", image_path, exc_info=True)
        return None

    over_r = round(over, 3)
    under_r = round(under, 3)
    return {
        "sharpness": sharpness,
        "overexposed_ratio": over_r,
        "underexposed_ratio": under_r,
        "flags": classify_quality_flags(sharpness, over_r, under_r),
    }


def _unique_dest(dest_dir: Path, filename: str) -> Path:
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    return dest_dir / f"{stem}_{uuid.uuid4().hex[:6]}{suffix}"


def _transfer_file(src: str, dest: Path, mode: str) -> str:
    if mode == "hardlink":
        try:
            os.link(src, dest)
            return "hardlink"
        except OSError:
            # Cross-filesystem or unsupported FS -> gracefully fall back to copy
            shutil.copy2(src, dest)
            return "copy"
    shutil.copy2(src, dest)
    return "copy"


MAX_ORIGINAL_EXPORT_PHOTOS = 500

_FORBIDDEN_EXPORT_DIR_NAMES = frozenset({".photo-review-cache", ".photo-trash"})
_FORBIDDEN_SYSTEM_PREFIXES = (
    "/System",
    "/bin",
    "/sbin",
    "/usr",
    "/etc",
    "/private/etc",
    "/private/var/db",
    "/Library",
    "/Applications",
    "/dev",
    "/proc",
    "/sys",
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\ProgramData",
)


def validate_export_target_dir(target_dir: str) -> Path:
    """Validate and resolve the destination directory for original photo export."""
    clean_dir = (target_dir or "").strip()
    if not clean_dir:
        raise ValueError("请指定导出目标文件夹")
    dest_root = Path(os.path.expanduser(clean_dir)).resolve()
    if dest_root == dest_root.parent:
        raise ValueError("不能直接导出到磁盘根目录，请选择子文件夹")
    if any(part in _FORBIDDEN_EXPORT_DIR_NAMES for part in dest_root.parts):
        raise ValueError("不能导出到相册内部缓存或回收站目录")
    norm_dest = os.path.normcase(str(dest_root))
    for prefix in _FORBIDDEN_SYSTEM_PREFIXES:
        norm_prefix = os.path.normcase(os.path.normpath(prefix))
        if norm_dest == norm_prefix or norm_dest.startswith(norm_prefix + os.sep):
            raise ValueError(f"出于安全保护，禁止导出到系统目录：{prefix}")
    return dest_root


def export_originals_to_dir(
    photos: list[dict[str, Any]],
    target_dir: str,
    mode: str = "copy",
    include_raw: bool = True,
) -> dict[str, Any]:
    """Export selected original photos (and optional RAW siblings) for external editing.

    Never modifies or deletes source files.
    """
    if len(photos) > MAX_ORIGINAL_EXPORT_PHOTOS:
        raise ValueError(
            f"单次最多导出 {MAX_ORIGINAL_EXPORT_PHOTOS} 张原图（当前选中 {len(photos)} 张），请分批导出"
        )
    dest_root = validate_export_target_dir(target_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    mode_norm = "hardlink" if (mode or "").strip().lower() == "hardlink" else "copy"

    exported = 0
    raw_exported = 0
    skipped = 0
    errors: list[dict[str, Any]] = []

    for photo in photos:
        src = photo.get("path") or photo.get("original_path") or ""
        if not src or not os.path.exists(src):
            skipped += 1
            errors.append({"id": photo.get("id"), "error": "原图文件不存在"})
            continue
        try:
            dest = _unique_dest(dest_root, Path(src).name)
            _transfer_file(src, dest, mode_norm)
            exported += 1
        except OSError as exc:
            errors.append({"id": photo.get("id"), "error": str(exc)})
            continue

        if include_raw:
            raw_path = photo.get("raw_path") or ""
            if raw_path and os.path.exists(raw_path) and os.path.abspath(raw_path) != os.path.abspath(src):
                try:
                    raw_dest = _unique_dest(dest_root, Path(raw_path).name)
                    _transfer_file(raw_path, raw_dest, mode_norm)
                    raw_exported += 1
                except OSError as exc:
                    errors.append({"id": photo.get("id"), "error": f"RAW 导出失败: {exc}"})

    return {
        "target_dir": str(dest_root),
        "mode": mode_norm,
        "exported": exported,
        "raw_exported": raw_exported,
        "skipped": skipped,
        "errors": errors,
    }
