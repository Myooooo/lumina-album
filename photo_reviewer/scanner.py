"""Folder scanning and memory album analysis."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .cache import cleanup_folder_cache, load_scan_results_from_cache
from .config import Config
from .db import Database
from .exif import extract_exif
from .geocode import reverse_geocode
from .pipeline import (
    analyze_prepared,
    base_record,
    is_coordinate_location,
    persist_analysis,
    prepare_for_analysis,
    prepare_proxy,
)

IMAGE_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
)

RAW_EXTENSIONS = {
    ".raw",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".dng",
    ".orf",
    ".rw2",
    ".pef",
    ".srw",
}


@dataclass
class ScanJob:
    id: str
    folder: str
    force: bool = False
    status: str = "pending"  # pending, running, completed, cancelled, error
    total: int = 0
    processed: int = 0
    current: str = ""
    phase: str = "index"
    error: str | None = None
    cancelled: bool = False


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, ScanJob] = {}
        self._lock = threading.Lock()

    def create(self, folder: str, force: bool = False) -> ScanJob:
        job = ScanJob(id=uuid.uuid4().hex[:12], folder=folder, force=force)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> ScanJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[ScanJob]:
        with self._lock:
            return list(self._jobs.values())

    def update(self, job_id: str, **kwargs) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                for k, v in kwargs.items():
                    setattr(job, k, v)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status == "running":
                job.cancelled = True
                return True
            return False


JOBS = JobManager()

logger = logging.getLogger(__name__)


def _friendly_error(exc: Exception) -> str:
    """Turn low-level exceptions into a short, human-friendly Chinese message."""
    raw = str(exc)
    lowered = raw.lower()
    if "cannot identify image" in lowered or "unidentifiedimage" in lowered:
        return "无法读取这张照片，文件可能已损坏或格式不受支持。"
    if "no such file" in lowered or "filenotfound" in lowered:
        return "照片文件不存在或已被移动。"
    if "无法连接本地模型服务" in raw or "模型接口" in raw:
        return raw[:500]
    return raw[:1000]


def discover_images(
    folder: str, extensions=IMAGE_EXTENSIONS, skip_dirs=None
) -> list[str]:
    folder_path = Path(folder)
    if not folder_path.exists() or not folder_path.is_dir():
        raise NotADirectoryError(f"文件夹不存在或不是目录: {folder}")
    exts = {e.lower() for e in extensions}
    skip = {".photo-trash", ".git", "__pycache__"}
    if skip_dirs:
        for d in skip_dirs:
            if not d:
                continue
            skip.add(os.path.abspath(d))
            name = os.path.basename(os.path.normpath(d))
            if name.startswith("."):
                skip.add(name)
    found = []
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [
            d
            for d in dirs
            if d not in skip and os.path.abspath(os.path.join(root, d)) not in skip
        ]
        parts = Path(root).parts
        if any(p in {".photo-trash", ".git", "__pycache__"} for p in parts):
            continue
        for name in files:
            suffix = Path(name).suffix.lower()
            if suffix in exts and suffix not in RAW_EXTENSIONS:
                found.append(os.path.join(root, name))
    found.sort()
    return found


def start_scan(
    folder: str, db: Database, config: Config, force: bool = False
) -> ScanJob:
    job = JOBS.create(folder, force)
    # Existing folders start in analysis phase; the worker indexes any
    # newly discovered files before calling the model.
    job.phase = "analyze" if db.paths_for_folder(folder) else "index"
    thread = threading.Thread(
        target=_scan_worker,
        args=(job.id, folder, db, config, force),
        daemon=True,
    )
    thread.start()
    return job


def _record_photo_error(
    image_path: str, folder: str, db: Database, exc: Exception
) -> None:
    """Record a processing error without throwing away an existing good analysis."""
    message = _friendly_error(exc)
    existing = db.get_photo_by_path(image_path)
    if existing:
        record = dict(existing)
        record["error"] = message
        if existing.get("status") in (None, "pending", "error"):
            record["status"] = "error"
        db.upsert_photo(record)
        return
    try:
        size = os.path.getsize(image_path)
    except OSError:
        size = 0
    db.upsert_photo(
        {
            "path": image_path,
            "folder": os.path.abspath(folder),
            "filename": os.path.basename(image_path),
            "size": size,
            "status": "error",
            "error": message,
        }
    )


def _preserve_resolved_location(
    location: str | None, existing: dict | None
) -> str | None:
    """Keep an existing human-readable address when EXIF only has coordinates."""
    if location is None and existing and existing.get("location"):
        return existing["location"]
    if (
        location
        and is_coordinate_location(location)
        and existing
        and existing.get("location")
        and not is_coordinate_location(existing["location"])
    ):
        return existing["location"]
    return location


def _resolve_location(
    location: str | None,
    exif: dict,
    existing: dict | None,
    config: Config,
) -> str | None:
    """Resolve coordinates to a place name, preserving an older address on failure."""
    if exif.get("latitude") is not None and exif.get("longitude") is not None:
        place = reverse_geocode(
            exif["latitude"],
            exif["longitude"],
            config.geocoding_provider,
            config.geocoding_api_key,
            interval=config.geocoding_interval,
            retries=config.geocoding_retries,
        )
        if place:
            return place
    return _preserve_resolved_location(location, existing)


def _analyze_one(image_path: str, folder: str, db: Database, config: Config) -> bool:
    """Analyze one photo and persist the result; errors are recorded per photo."""
    try:
        existing = db.get_photo_by_path(image_path)
        prepared = prepare_for_analysis(
            image_path,
            folder,
            config,
            existing.get("location") if existing else None,
        )
        if existing:
            db.update_exif(existing["id"], prepared.location, prepared.exif)
        result = analyze_prepared(prepared, config)
        persist_analysis(db, prepared, result, config)
        return True
    except Exception as exc:  # noqa: BLE001
        _record_photo_error(image_path, folder, db, exc)
        return False


def _index_one(
    image_path: str,
    folder: str,
    db: Database,
    config: Config,
    refresh_exif: bool = False,
    resolve_location: bool = False,
) -> bool:
    """Generate one proxy and upsert its index row."""
    try:
        prepared = prepare_proxy(image_path, folder, config)
        existing = db.get_photo_by_path(prepared.image_path)
        if refresh_exif:
            location, exif = extract_exif(image_path)
            if resolve_location:
                location = _resolve_location(location, exif, existing, config)
            else:
                location = _preserve_resolved_location(location, existing)
            if location:
                exif["location"] = location
            if location and not is_coordinate_location(location):
                exif["geocoded"] = True
            prepared.location = location
            prepared.exif = exif or {}
        status = existing.get("status") if existing else "pending"
        record = base_record(prepared, existing=existing, status=status)
        if refresh_exif:
            record["location"] = prepared.location
            record["exif"] = prepared.exif
        db.upsert_photo(record)
        return True
    except Exception as exc:  # noqa: BLE001
        _record_photo_error(image_path, folder, db, exc)
        return False


def _run_index_phase(
    job_id: str,
    images: list[str],
    folder: str,
    db: Database,
    config: Config,
    refresh_exif: bool = False,
    resolve_location: bool = False,
) -> bool:
    """Build proxies for a list of images with bounded concurrency."""
    JOBS.update(job_id, phase="index", total=len(images), processed=0, current="")
    workers = max(1, min(32, int(config.index_concurrency or 4)))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="lumina-index"
    ) as executor:
        futures = {
            executor.submit(
                _index_one,
                image_path,
                folder,
                db,
                config,
                refresh_exif,
                resolve_location,
            ): image_path
            for image_path in images
        }
        for processed, future in enumerate(as_completed(futures), start=1):
            image_path = futures[future]
            JOBS.update(job_id, processed=processed, current=image_path)
            job = JOBS.get(job_id)
            if not job or job.cancelled:
                JOBS.update(job_id, status="cancelled")
                for pending in futures:
                    pending.cancel()
                return False
    return True


def _scan_worker(
    job_id: str, folder: str, db: Database, config: Config, force: bool
) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    try:
        load_scan_results_from_cache(folder, db, config.cache_dir_name)
        active_paths = set(db.paths_for_folder(folder))
        JOBS.update(
            job_id,
            status="running",
            phase="index" if not active_paths else "analyze",
            error=None,
        )
        skip_dirs = [config.data_dir, config.trash_dir_name, config.cache_dir_name]
        images = discover_images(folder, config.image_extensions, skip_dirs=skip_dirs)

        # Index every photo we have not seen yet. This also covers files added
        # after a previous scan, including a forced re-scan.
        if not active_paths:
            to_index = list(images)
        else:
            to_index = [
                image_path for image_path in images if image_path not in active_paths
            ]

        if to_index and not _run_index_phase(
            job_id,
            to_index,
            folder,
            db,
            config,
            refresh_exif=True,
            resolve_location=True,
        ):
            return

        # Analyze pending photos, or every photo when the user forced a full
        # re-analysis. Corrupted files stay in the queue so they can be retried
        # after the file is repaired.
        if force:
            to_analyze = [
                image_path for image_path in images if db.get_photo_by_path(image_path)
            ]
        else:
            to_analyze = []
            for image_path in images:
                existing = db.get_photo_by_path(image_path)
                if existing and not (
                    existing.get("status") == "analyzed"
                    and existing.get("score") is not None
                ):
                    to_analyze.append(image_path)

        JOBS.update(
            job_id, phase="analyze", total=len(to_analyze), processed=0, current=""
        )
        workers = max(1, int(config.scan_concurrency or 1))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="lumina-analyze"
        ) as executor:
            futures = {
                executor.submit(
                    _analyze_one, image_path, folder, db, config
                ): image_path
                for image_path in to_analyze
            }
            for processed, future in enumerate(as_completed(futures), start=1):
                image_path = futures[future]
                JOBS.update(job_id, processed=processed, current=image_path)
                job = JOBS.get(job_id)
                if not job or job.cancelled:
                    JOBS.update(job_id, status="cancelled")
                    for pending in futures:
                        pending.cancel()
                    return

        if not JOBS.get(job_id).cancelled:
            try:
                cleanup_folder_cache(folder, db, config.cache_dir_name)
                backup_path = (
                    Path(__file__).resolve().parent.parent / "photo-library.backup.db"
                )
                db.backup_to(str(backup_path))
            except (OSError, sqlite3.Error):
                logger.warning(
                    "post-scan cleanup/backup failed for %s", folder, exc_info=True
                )
            JOBS.update(job_id, status="completed")
    except Exception as exc:  # noqa: BLE001
        JOBS.update(job_id, status="error", error=str(exc))


def start_rebuild_index(folder: str, db: Database, config: Config) -> ScanJob:
    job = JOBS.create(folder, False)
    thread = threading.Thread(
        target=_rebuild_worker,
        args=(job.id, folder, db, config),
        daemon=True,
    )
    thread.start()
    return job


def _rebuild_worker(job_id: str, folder: str, db: Database, config: Config) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    JOBS.update(job_id, status="running", phase="index", error=None)
    try:
        skip_dirs = [config.data_dir, config.trash_dir_name, config.cache_dir_name]
        images = discover_images(folder, config.image_extensions, skip_dirs=skip_dirs)
        current_set = set(images)
        active_paths = set(db.paths_for_folder(folder))
        removed = active_paths - current_set
        added = [image_path for image_path in images if image_path not in active_paths]

        # Deleted files: remove their DB row and their proxy cache immediately.
        for path in removed:
            try:
                row = db.get_photo_by_path(path)
                if row:
                    for key in ("thumb_path", "proxy_path"):
                        cached = row.get(key)
                        if cached and os.path.exists(cached):
                            try:
                                os.remove(cached)
                            except OSError:
                                logger.debug("proxy file is already gone: %s", cached)
                    db.delete_rows([row["id"]])
            except sqlite3.Error:
                logger.warning(
                    "could not remove stale photo row for %s", path, exc_info=True
                )

        # Already indexed photos are skipped: only newly discovered files are
        # indexed here (proxy + EXIF), without touching existing results.
        if not _run_index_phase(
            job_id,
            added,
            folder,
            db,
            config,
            refresh_exif=True,
            resolve_location=True,
        ):
            return
        if not JOBS.get(job_id).cancelled:
            try:
                cleanup_folder_cache(folder, db, config.cache_dir_name)
                backup_path = (
                    Path(__file__).resolve().parent.parent / "photo-library.backup.db"
                )
                db.backup_to(str(backup_path))
            except (OSError, sqlite3.Error):
                logger.warning(
                    "post-scan cleanup/backup failed for %s", folder, exc_info=True
                )
            JOBS.update(job_id, status="completed")
    except Exception as exc:  # noqa: BLE001
        JOBS.update(job_id, status="error", error=str(exc))


def reanalyze_photo(photo_id: int, db: Database, config: Config) -> dict:
    """Re-analyze a single photo (used by the UI's re-analyze button)."""
    photo = db.get_photo(photo_id)
    if not photo:
        raise ValueError("照片不存在")
    if photo.get("status") == "deleted":
        raise ValueError("已删除的照片不能重新分析")
    image_path = photo.get("path") or photo.get("original_path")
    if not image_path or not os.path.exists(image_path):
        raise FileNotFoundError(f"文件不存在: {image_path}")

    folder = photo.get("folder") or os.path.dirname(image_path)
    try:
        prepared = prepare_for_analysis(
            image_path,
            folder,
            config,
            photo.get("location"),
        )
        # Sync metadata first so camera/EXIF/location are refreshed even if
        # the model call fails afterwards.
        db.update_exif(photo_id, prepared.location, prepared.exif)
        result = analyze_prepared(prepared, config)
        persist_analysis(db, prepared, result, config)
    except Exception as exc:
        _record_photo_error(image_path, folder, db, exc)
        raise
    return db.get_photo(photo_id)
