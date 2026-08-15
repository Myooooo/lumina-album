"""Folder scanning and memory album analysis."""
from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .api_client import analyze_image, build_analysis_context
from .cache import cleanup_folder_cache, ensure_cache_dirs, load_scan_results_from_cache
from .config import Config
from .db import Database
from .exif import extract_exif
from .geocode import reverse_geocode
from .thumbnailer import make_proxy

IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
)

RAW_EXTENSIONS = {
    ".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".pef", ".srw",
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
    error: Optional[str] = None
    cancelled: bool = False


class JobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, ScanJob] = {}
        self._lock = threading.Lock()

    def create(self, folder: str, force: bool = False) -> ScanJob:
        job = ScanJob(id=uuid.uuid4().hex[:12], folder=folder, force=force)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[ScanJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[ScanJob]:
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


def discover_images(folder: str, extensions=IMAGE_EXTENSIONS, skip_dirs=None) -> List[str]:
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
        dirs[:] = [d for d in dirs if d not in skip and os.path.abspath(os.path.join(root, d)) not in skip]
        parts = Path(root).parts
        if any(p in {".photo-trash", ".git", "__pycache__"} for p in parts):
            continue
        for name in files:
            suffix = Path(name).suffix.lower()
            if suffix in exts and suffix not in RAW_EXTENSIONS:
                found.append(os.path.join(root, name))
    found.sort()
    return found


def start_scan(folder: str, db: Database, config: Config, force: bool = False) -> ScanJob:
    job = JOBS.create(folder, force)
    # If the folder already has an index, skip the proxy/index phase.
    job.phase = "analyze" if db.paths_for_folder(folder) else "index"
    thread = threading.Thread(
        target=_scan_worker,
        args=(job.id, folder, db, config, force),
        daemon=True,
    )
    thread.start()
    return job


def _scan_worker(job_id: str, folder: str, db: Database, config: Config, force: bool) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    active_paths = set(db.paths_for_folder(folder))
    first_time = len(active_paths) == 0
    JOBS.update(job_id, status="running", phase="index" if first_time else "analyze", error=None)
    try:
        load_scan_results_from_cache(folder, db, config.cache_dir_name)
        skip_dirs = [config.data_dir, config.trash_dir_name, config.cache_dir_name]
        images = discover_images(folder, config.image_extensions, skip_dirs=skip_dirs)
        thumb_dir = ensure_cache_dirs(folder, config.cache_dir_name)

        # Only the first scan builds the index and generates proxies. Later
        # scans only call the LLM on photos that already exist in the index
        # and do not have a score yet.
        if first_time:
            # Phase 1: build/refresh index and generate proxies for every photo.
            JOBS.update(job_id, phase="index", total=len(images), processed=0, current="")
            for idx, image_path in enumerate(images, start=1):
                job = JOBS.get(job_id)
                if not job or job.cancelled:
                    JOBS.update(job_id, status="cancelled")
                    return
                JOBS.update(job_id, processed=idx, current=image_path)
                try:
                    proxy_path, width, height, phash, thumb_path = make_proxy(
                        image_path,
                        max_edge=config.proxy_max_edge,
                        thumb_dir=str(thumb_dir),
                        quality=config.proxy_quality,
                        thumb_size=config.thumb_size,
                    )
                    existing = db.get_photo_by_path(image_path)
                    record = {
                        "path": image_path,
                        "folder": os.path.abspath(folder),
                        "filename": os.path.basename(image_path),
                        "size": os.path.getsize(image_path),
                        "width": width,
                        "height": height,
                        "thumb_path": thumb_path,
                        "proxy_path": proxy_path,
                        "phash": phash,
                        "status": existing.get("status") if existing else "pending",
                    }
                    if existing:
                        for key in ("score", "recommendation", "tags", "reason", "model", "dimensions", "location", "exif", "analyzed_at", "favorite", "error"):
                            if existing.get(key) is not None:
                                record[key] = existing[key]
                    db.upsert_photo(record)
                except Exception as exc:  # noqa: BLE001
                    try:
                        existing = db.get_photo_by_path(image_path)
                        if existing:
                            db.mark_error(existing["id"], str(exc)[:1000])
                        else:
                            db.upsert_photo({
                                "path": image_path,
                                "folder": os.path.abspath(folder),
                                "filename": os.path.basename(image_path),
                                "size": os.path.getsize(image_path),
                                "status": "error",
                                "error": str(exc)[:1000],
                            })
                    except Exception:
                        pass

            if JOBS.get(job_id).cancelled:
                JOBS.update(job_id, status="cancelled")
                return

        # Phase 2: analyze only photos without a score (unless forced).
        if force:
            to_analyze = []
            for image_path in images:
                if db.get_photo_by_path(image_path):
                    to_analyze.append(image_path)
        else:
            to_analyze = []
            for image_path in images:
                existing = db.get_photo_by_path(image_path)
                if existing and not (existing.get("status") == "analyzed" and existing.get("score") is not None):
                    to_analyze.append(image_path)

        JOBS.update(job_id, phase="analyze", total=len(to_analyze), processed=0, current="")
        for idx, image_path in enumerate(to_analyze, start=1):
            job = JOBS.get(job_id)
            if not job or job.cancelled:
                JOBS.update(job_id, status="cancelled")
                return
            JOBS.update(job_id, processed=idx, current=image_path)
            try:
                proxy_path, width, height, phash, thumb_path = make_proxy(
                    image_path,
                    max_edge=config.proxy_max_edge,
                    thumb_dir=str(thumb_dir),
                    quality=config.proxy_quality,
                    thumb_size=config.thumb_size,
                )
                location, exif = extract_exif(image_path)
                if exif.get("latitude") is not None and exif.get("longitude") is not None:
                    place = reverse_geocode(
                        exif["latitude"], exif["longitude"],
                        config.geocoding_provider, config.geocoding_api_key,
                    )
                    if place:
                        location = place
                result = analyze_image(
                    proxy_path,
                    config,
                    context=build_analysis_context(location, exif),
                )
                record = {
                    "path": image_path,
                    "folder": os.path.abspath(folder),
                    "filename": os.path.basename(image_path),
                    "size": os.path.getsize(image_path),
                    "width": width,
                    "height": height,
                    "thumb_path": thumb_path,
                    "proxy_path": proxy_path,
                    "dimensions": result.get("dimensions", {}),
                    "location": location,
                    "exif": exif,
                    "phash": phash,
                    "score": result["score"],
                    "recommendation": result.get("recommendation"),
                    "tags": result["tags"],
                    "reason": result.get("reason") or result.get("comment"),
                    "model": config.model,
                    "analyzed_at": None,
                    "status": "analyzed",
                    "error": None,
                }
                photo_id = db.upsert_photo(record)
                db.update_analysis(
                    photo_id,
                    score=result["score"],
                    recommendation=result.get("recommendation"),
                    tags=result["tags"],
                    reason=result.get("reason") or result.get("comment"),
                    model=config.model,
                    phash=phash,
                    width=width,
                    height=height,
                    thumb_path=thumb_path,
                    proxy_path=proxy_path,
                    dimensions=result.get("dimensions", {}),
                    location=location,
                    exif=exif,
                    status="analyzed",
                )
            except Exception as exc:  # noqa: BLE001
                try:
                    existing = db.get_photo_by_path(image_path)
                    if existing:
                        db.mark_error(existing["id"], str(exc)[:1000])
                    else:
                        db.upsert_photo({
                            "path": image_path,
                            "folder": os.path.abspath(folder),
                            "filename": os.path.basename(image_path),
                            "size": os.path.getsize(image_path),
                            "status": "error",
                            "error": str(exc)[:1000],
                        })
                except Exception:
                    pass

        if not JOBS.get(job_id).cancelled:
            try:
                cleanup_folder_cache(folder, db, config.cache_dir_name)
                backup_path = Path(__file__).resolve().parent.parent / "photo-library.backup.db"
                db.backup_to(str(backup_path))
            except Exception:
                pass
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
        for path in removed:
            try:
                row = db.get_photo_by_path(path)
                if row:
                    db.delete_rows([row["id"]])
            except Exception:
                pass

        thumb_dir = ensure_cache_dirs(folder, config.cache_dir_name)
        JOBS.update(job_id, phase="index", total=len(images), processed=0, current="")
        for idx, image_path in enumerate(images, start=1):
            job = JOBS.get(job_id)
            if not job or job.cancelled:
                JOBS.update(job_id, status="cancelled")
                return
            JOBS.update(job_id, processed=idx, current=image_path)
            try:
                proxy_path, width, height, phash, thumb_path = make_proxy(
                    image_path,
                    max_edge=config.proxy_max_edge,
                    thumb_dir=str(thumb_dir),
                    quality=config.proxy_quality,
                    thumb_size=config.thumb_size,
                )
                existing = db.get_photo_by_path(image_path)
                record = {
                    "path": image_path,
                    "folder": os.path.abspath(folder),
                    "filename": os.path.basename(image_path),
                    "size": os.path.getsize(image_path),
                    "width": width,
                    "height": height,
                    "thumb_path": thumb_path,
                    "proxy_path": proxy_path,
                    "phash": phash,
                    "status": existing.get("status") if existing else "pending",
                }
                if existing:
                    for key in ("score", "recommendation", "tags", "reason", "model", "dimensions", "location", "exif", "analyzed_at", "favorite", "error"):
                        if existing.get(key) is not None:
                            record[key] = existing[key]
                db.upsert_photo(record)
            except Exception as exc:  # noqa: BLE001
                try:
                    existing = db.get_photo_by_path(image_path)
                    if existing:
                        db.mark_error(existing["id"], str(exc)[:1000])
                    else:
                        db.upsert_photo({
                            "path": image_path,
                            "folder": os.path.abspath(folder),
                            "filename": os.path.basename(image_path),
                            "size": os.path.getsize(image_path),
                            "status": "error",
                            "error": str(exc)[:1000],
                        })
                except Exception:
                    pass
        if not JOBS.get(job_id).cancelled:
            try:
                cleanup_folder_cache(folder, db, config.cache_dir_name)
                backup_path = Path(__file__).resolve().parent.parent / "photo-library.backup.db"
                db.backup_to(str(backup_path))
            except Exception:
                pass
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
    thumb_dir = ensure_cache_dirs(folder, config.cache_dir_name)
    proxy_path, width, height, phash, thumb_path = make_proxy(
        image_path,
        max_edge=config.proxy_max_edge,
        thumb_dir=str(thumb_dir),
        quality=config.proxy_quality,
        thumb_size=config.thumb_size,
    )
    location, exif = extract_exif(image_path)
    if exif.get("latitude") is not None and exif.get("longitude") is not None:
        place = reverse_geocode(
            exif["latitude"], exif["longitude"],
            config.geocoding_provider, config.geocoding_api_key,
        )
        if place:
            location = place
    result = analyze_image(
        proxy_path,
        config,
        context=build_analysis_context(location, exif),
    )
    db.update_analysis(
        photo_id,
        score=result["score"],
        recommendation=result.get("recommendation"),
        tags=result["tags"],
        reason=result.get("reason") or result.get("comment"),
        model=config.model,
        phash=phash,
        width=width,
        height=height,
        thumb_path=thumb_path,
        proxy_path=proxy_path,
        dimensions=result.get("dimensions", {}),
        location=location,
        exif=exif,
        status="analyzed",
    )
    return db.get_photo(photo_id)
