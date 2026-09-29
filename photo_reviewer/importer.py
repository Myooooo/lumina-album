"""Import photos from a camera card into an indexed local folder.

The local folder mirrors the card's directory structure, so a source file is
identified by its path relative to the import root. Two modes are supported:

``incremental``
    Copy only what the local folder does not have. Files that already exist are
    left untouched, and anything that exists locally but not on the card is
    never modified.

``full``
    Additionally refresh files that exist on both sides. A file whose size and
    modification time already match is considered identical and skipped, so a
    repeated import of an unchanged card is a no-op.

Camera raw files are handled as part of the same group as their ``jpg``/``png``
counterpart: the image file is what gets indexed and analysed, the raw file is
recorded as that photo's ``raw_path`` and shown as a badge in the UI. A raw
file with no same-named image is imported on its own.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .paths import canonical_path

logger = logging.getLogger(__name__)

INCREMENTAL = "incremental"
FULL = "full"
IMPORT_MODES = (INCREMENTAL, FULL)

#: Which members of a jpg+raw group to copy.
RAW_POLICY_IMAGE_FIRST = "image"  # image only; raw only when there is no image
RAW_POLICY_BOTH = "both"
RAW_POLICY_RAW_ONLY = "raw"
RAW_POLICIES = (RAW_POLICY_IMAGE_FIRST, RAW_POLICY_BOTH, RAW_POLICY_RAW_ONLY)

#: What to do after the files are in place.
AFTER_COPY_ONLY = "copy"
AFTER_INDEX = "index"
AFTER_ORGANIZE = "organize"
AFTER_CHOICES = (AFTER_COPY_ONLY, AFTER_INDEX, AFTER_ORGANIZE)

#: Upper bound on how many rows a single response describes in detail.
MAX_REPORTED_ITEMS = 200


@dataclass
class SourceFile:
    """One file on the card, with the relative path it will keep locally."""

    source_path: str
    relative_path: str
    size: int
    mtime: float
    is_raw: bool


@dataclass
class SourceGroup:
    """A photo on the card: an image file, a raw file, or both."""

    relative_dir: str
    stem: str
    image: SourceFile | None = None
    raw: SourceFile | None = None

    @property
    def key(self) -> str:
        return os.path.join(self.relative_dir, self.stem).lower()


@dataclass
class PlannedCopy:
    """One file that the import will write."""

    source_path: str
    target_path: str
    relative_path: str
    size: int
    action: str  # "add" or "overwrite"
    replaced_size: int | None = None
    content_changed: bool = False
    is_raw: bool = False

    def as_dict(self) -> dict:
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "action": self.action,
            "replaced_size": self.replaced_size,
            "content_changed": self.content_changed,
            "is_raw": self.is_raw,
        }


@dataclass
class ImportPlan:
    """The result of comparing a card against a local folder."""

    source: str
    target: str
    mode: str
    raw_policy: str
    copies: list[PlannedCopy] = field(default_factory=list)
    skipped: int = 0
    local_only: int = 0
    unpaired_raw: int = 0
    warnings: list[str] = field(default_factory=list)
    scanned_files: int = 0

    @property
    def added(self) -> list[PlannedCopy]:
        return [c for c in self.copies if c.action == "add"]

    @property
    def overwritten(self) -> list[PlannedCopy]:
        return [c for c in self.copies if c.action == "overwrite"]

    @property
    def total_bytes(self) -> int:
        return sum(c.size for c in self.copies)

    def summary(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "mode": self.mode,
            "raw_policy": self.raw_policy,
            "new_count": len(self.added),
            "overwrite_count": len(self.overwritten),
            "skip_count": self.skipped,
            "local_only_count": self.local_only,
            "unpaired_raw_count": self.unpaired_raw,
            "total_bytes": self.total_bytes,
            "scanned_files": self.scanned_files,
            "warnings": self.warnings,
            "items": [c.as_dict() for c in self.copies[:MAX_REPORTED_ITEMS]],
            "items_truncated": max(0, len(self.copies) - MAX_REPORTED_ITEMS),
        }


class ImportError_(RuntimeError):
    """Raised when an import cannot be planned or executed."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _skip_directory(name: str, skip_names: set[str]) -> bool:
    return name in skip_names or name.startswith(".")


def scan_source(
    source_root: str,
    image_extensions,
    raw_extensions,
    skip_names: set[str] | None = None,
) -> tuple[list[SourceGroup], int]:
    """Walk the card and group its files the way the album indexes them.

    Returns ``(groups, scanned_files)``. ``scanned_files`` counts every image
    and raw file seen, so the caller can tell the user how much was considered.
    """
    source_root = canonical_path(source_root)
    if not os.path.isdir(source_root):
        raise ImportError_(f"来源文件夹不存在: {source_root}")

    images = {str(e).lower() for e in image_extensions}
    raws = {str(e).lower() for e in raw_extensions}
    skip = set(skip_names or ()) | {".photo-trash", ".photo-review-cache"}

    groups: dict[str, SourceGroup] = {}
    scanned = 0
    for root, dirs, files in os.walk(source_root):
        dirs[:] = [d for d in dirs if not _skip_directory(d, skip)]
        relative_dir = os.path.relpath(root, source_root)
        if relative_dir == ".":
            relative_dir = ""
        for name in files:
            if name.startswith("._"):
                # macOS AppleDouble companion written when a card is read on a
                # Mac; it shares the real file's extension but is not a photo.
                continue
            suffix = Path(name).suffix.lower()
            if suffix in images:
                is_raw = False
            elif suffix in raws:
                is_raw = True
            else:
                continue
            full = os.path.join(root, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            scanned += 1
            entry = SourceFile(
                source_path=full,
                relative_path=os.path.join(relative_dir, name)
                if relative_dir
                else name,
                size=stat.st_size,
                mtime=stat.st_mtime,
                is_raw=is_raw,
            )
            stem = Path(name).stem.lower()
            key = os.path.join(relative_dir, stem).lower()
            group = groups.setdefault(
                key, SourceGroup(relative_dir=relative_dir, stem=Path(name).stem)
            )
            if is_raw:
                if group.raw is None:
                    group.raw = entry
            elif group.image is None:
                group.image = entry
    return list(groups.values()), scanned


def _selected_files(group: SourceGroup, raw_policy: str) -> list[SourceFile]:
    """Pick the members of a group that the policy wants to copy."""
    if raw_policy == RAW_POLICY_RAW_ONLY:
        chosen = [group.raw] if group.raw else [group.image]
    elif raw_policy == RAW_POLICY_BOTH:
        chosen = [group.image, group.raw]
    else:  # image first, raw only as a fallback
        chosen = [group.image] if group.image else [group.raw]
    return [f for f in chosen if f is not None]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def plan_import(
    source_root: str,
    target_root: str,
    config: Config,
    mode: str = INCREMENTAL,
    raw_policy: str = RAW_POLICY_IMAGE_FIRST,
) -> ImportPlan:
    """Compare a card against a local folder without touching either."""
    source = canonical_path(source_root)
    target = canonical_path(target_root)
    if mode not in IMPORT_MODES:
        raise ImportError_(f"未知的导入模式: {mode}")
    if raw_policy not in RAW_POLICIES:
        raise ImportError_(f"未知的 RAW 策略: {raw_policy}")
    if not os.path.isdir(target):
        raise ImportError_(f"目标文件夹不存在: {target}")
    if not source or not os.path.isdir(source):
        raise ImportError_(f"来源文件夹不存在: {source}")
    if source == target:
        raise ImportError_("来源和目标不能是同一个文件夹")
    if target.lower().startswith(source.lower() + os.sep):
        raise ImportError_("目标文件夹位于来源文件夹内部，请换一个目标目录")

    groups, scanned = scan_source(
        source, config.image_extensions, config.raw_extensions, {config.cache_dir_name}
    )

    plan = ImportPlan(
        source=source,
        target=target,
        mode=mode,
        raw_policy=raw_policy,
        scanned_files=scanned,
    )

    for group in sorted(groups, key=lambda g: g.key):
        if group.image is None and group.raw is not None:
            plan.unpaired_raw += 1
        for entry in _selected_files(group, raw_policy):
            target_path = os.path.join(target, entry.relative_path)
            existing = _stat_or_none(target_path)
            if existing is None:
                plan.copies.append(
                    PlannedCopy(
                        source_path=entry.source_path,
                        target_path=target_path,
                        relative_path=entry.relative_path,
                        size=entry.size,
                        action="add",
                        is_raw=entry.is_raw,
                    )
                )
                continue
            identical = (
                existing.st_size == entry.size
                and abs(existing.st_mtime - entry.mtime) < 1.0
            )
            if identical:
                plan.skipped += 1
                continue
            if mode == FULL:
                plan.copies.append(
                    PlannedCopy(
                        source_path=entry.source_path,
                        target_path=target_path,
                        relative_path=entry.relative_path,
                        size=entry.size,
                        action="overwrite",
                        replaced_size=existing.st_size,
                        content_changed=True,
                        is_raw=entry.is_raw,
                    )
                )
            else:
                plan.skipped += 1

    plan.local_only = _count_local_only(target, source, groups, config, raw_policy)

    if plan.unpaired_raw:
        plan.warnings.append(
            f"卡上有 {plan.unpaired_raw} 个 RAW 文件没有同名图片，它们会作为独立照片导入。"
        )
    collisions = _case_collisions(plan.copies)
    if collisions:
        plan.warnings.append(
            "有 {} 个目标路径仅在大小写上不同，Windows 上会互相覆盖：{}".format(
                len(collisions), "、".join(sorted(collisions)[:3])
            )
        )
    return plan


def _stat_or_none(path: str):
    try:
        return os.stat(path)
    except OSError:
        return None


def _count_local_only(
    target: str, source: str, groups: list[SourceGroup], config: Config, raw_policy: str
) -> int:
    """Count files the local folder has that the card does not.

    These are never touched; the number is reported so the user can see that
    the import is not about to delete anything.
    """
    wanted = set()
    for group in groups:
        for entry in _selected_files(group, raw_policy):
            wanted.add(os.path.normcase(os.path.join(target, entry.relative_path)))
    images = {str(e).lower() for e in config.image_extensions}
    raws = {str(e).lower() for e in config.raw_extensions}
    count = 0
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("._"):
                continue
            suffix = Path(name).suffix.lower()
            if suffix not in images and suffix not in raws:
                continue
            full = os.path.join(root, name)
            if os.path.normcase(full) not in wanted:
                count += 1
    return count


def _case_collisions(copies: list[PlannedCopy]) -> set[str]:
    seen: dict[str, str] = {}
    collisions: set[str] = set()
    for copy in copies:
        key = os.path.normcase(copy.target_path)
        previous = seen.get(key)
        if previous is not None and previous != copy.target_path:
            collisions.add(copy.relative_path)
        seen[key] = copy.target_path
    return collisions


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _copy_one(copy: PlannedCopy) -> None:
    parent = os.path.dirname(copy.target_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # ``copy2`` keeps the modification time, which matters twice over: the
    # album's proxy cache key includes mtime, and the next import uses
    # (size, mtime) to recognise an unchanged file.
    shutil.copy2(copy.source_path, copy.target_path)


def check_space(plan: ImportPlan) -> None:
    """Fail early when the destination cannot hold the whole import."""
    needed = plan.total_bytes
    if not needed:
        return
    target_root = plan.target
    while target_root and not os.path.isdir(target_root):
        parent = os.path.dirname(target_root)
        if parent == target_root:
            break
        target_root = parent
    try:
        usage = shutil.disk_usage(target_root)
    except OSError:
        return
    # Keep a small headroom so a nearly full disk does not fail mid-copy.
    if usage.free < needed * 1.02 + 32 * 1024 * 1024:
        needed_gb = needed / 1073741824
        free_gb = usage.free / 1073741824
        raise ImportError_(
            f"目标磁盘空间不足：需要 {needed_gb:.2f} GB，可用 {free_gb:.2f} GB"
        )


def run_import(
    plan: ImportPlan,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, str], None] | None = None,
) -> dict:
    """Execute a plan, returning a report.

    ``should_cancel`` is polled between files, so a cancelled import stops at a
    file boundary instead of leaving a half-written image behind.
    """
    check_space(plan)
    copied = 0
    failed: list[dict] = []
    cancelled = False
    total = len(plan.copies)

    for index, copy in enumerate(plan.copies, start=1):
        if should_cancel and should_cancel():
            cancelled = True
            break
        try:
            _copy_one(copy)
            copied += 1
        except OSError as exc:
            logger.warning("import failed for %s", copy.source_path, exc_info=True)
            failed.append({"relative_path": copy.relative_path, "error": str(exc)})
        if on_progress:
            on_progress(index, copy.relative_path)

    return {
        "copied": copied,
        "failed": failed,
        "cancelled": cancelled,
        "total": total,
        "bytes_copied": sum(c.size for c in plan.copies[:copied]),
    }
