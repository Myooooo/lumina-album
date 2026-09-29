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
from .paths import canonical_path
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

# Camera raw formats. A raw file that sits next to a same-named image file is
# recorded as that photo's ``raw_path`` instead of becoming its own entry; a
# raw file with no counterpart is indexed on its own and rendered from the
# embedded JPEG preview.
RAW_EXTENSIONS = {
    ".nef",
    ".arw",
    ".cr2",
    ".cr3",
    ".nrw",
    ".dng",
    ".orf",
    ".rw2",
    ".raf",
    ".pef",
    ".srw",
    ".raw",
    ".rwl",
    ".3fr",
    ".iiq",
    ".mos",
    ".mrw",
    ".k25",
    ".kdc",
    ".dcr",
    ".x3f",
    ".erf",
    ".mef",
    ".sr2",
    ".srf",
    ".cap",
    ".fff",
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
    # Extra context for import jobs: how many files were copied and what the
    # user asked to happen afterwards.
    detail: str = ""
    report: dict | None = None


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


def is_metadata_file(name: str) -> bool:
    """Return True for sidecar/metadata files that only look like photos.

    ``._DSC_0001.NEF`` is the macOS AppleDouble companion a card picks up when
    it has been read on a Mac. It is 4 KB of resource-fork metadata with the
    same extension as the real file, so it must never be indexed: it would
    either shadow the actual photo or show up as a broken one.
    """
    return name.startswith("._")


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


def discover_photo_pairs(
    folder: str,
    extensions=IMAGE_EXTENSIONS,
    raw_extensions: set | None = None,
    skip_dirs=None,
) -> list[tuple[str, str | None]]:
    """Return ``(image_path, raw_sibling_or_None)`` for every photo to index.

    A camera raw file next to a same-named ``jpg``/``png``/... is *not* a
    separate photo: the image file is what gets rendered and analysed, and the
    raw path is only remembered so the UI can show a "RAW" badge.

    A raw file without a same-named counterpart is indexed on its own; it is
    rendered from its embedded JPEG preview.

    Files whose name starts with ``._`` are skipped: those are macOS
    AppleDouble resource forks (``._DSC_0001.NEF``), not photographs. They
    share a stem with the real file and would otherwise shadow it or be indexed
    as a broken raw of their own.
    """
    folder_path = Path(folder)
    if not folder_path.exists() or not folder_path.is_dir():
        raise NotADirectoryError(f"文件夹不存在或不是目录: {folder}")
    exts = {e.lower() for e in extensions}
    raws = {e.lower() for e in (raw_extensions if raw_extensions is not None else RAW_EXTENSIONS)}
    skip = {".photo-trash", ".git", "__pycache__"}
    if skip_dirs:
        for d in skip_dirs:
            if not d:
                continue
            skip.add(os.path.abspath(d))
            name = os.path.basename(os.path.normpath(d))
            if name.startswith("."):
                skip.add(name)

    by_stem: dict[tuple[str, str], dict[str, str]] = {}
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
            if is_metadata_file(name):
                continue
            suffix = Path(name).suffix.lower()
            if suffix in exts:
                kind = "image"
            elif suffix in raws:
                kind = "raw"
            else:
                continue
            key = (root, Path(name).stem.lower())
            by_stem.setdefault(key, {})[kind] = os.path.join(root, name)

    pairs: list[tuple[str, str | None]] = []
    for entry in by_stem.values():
        image = entry.get("image")
        raw = entry.get("raw")
        if image:
            pairs.append((image, raw))
        elif raw:
            # Raw file with no same-named image: it becomes the photo itself.
            pairs.append((raw, None))
    pairs.sort(key=lambda item: item[0])
    return pairs


def discover_images(
    folder: str,
    extensions=IMAGE_EXTENSIONS,
    raw_extensions: set | None = None,
    skip_dirs=None,
) -> list[str]:
    """Return the primary file path of every photo in ``folder``."""
    return [
        image_path
        for image_path, _raw in discover_photo_pairs(
            folder, extensions, raw_extensions, skip_dirs
        )
    ]


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


def start_scoped_scan(
    folder: str,
    db: Database,
    config: Config,
    only_paths: list[str],
    analyze: bool = True,
) -> ScanJob:
    """Index (and optionally analyse) an explicit list of files.

    Used by the SD-card import so that bringing in a card never re-queues the
    thousands of photos that are already sitting in the library, and so the
    default can be "index only, no model calls".
    """
    job = JOBS.create(folder, False)
    job.phase = "index"
    thread = threading.Thread(
        target=_scan_worker,
        args=(job.id, folder, db, config, False, only_paths, analyze),
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
            raw_path=existing.get("raw_path") if existing else None,
            already_geocoded=bool(existing and existing.get("geocoded")),
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
    raw_path: str | None = None,
) -> bool:
    """Generate one proxy and upsert its index row."""
    try:
        prepared = prepare_proxy(image_path, folder, config, raw_path=raw_path)
        existing = db.get_photo_by_path(prepared.image_path)
        geocoded = False
        if refresh_exif:
            location, exif = extract_exif(image_path)
            if resolve_location:
                location = _resolve_location(location, exif, existing, config)
                geocoded = bool(location) and not is_coordinate_location(location)
            else:
                location = _preserve_resolved_location(location, existing)
                geocoded = bool(existing and existing.get("geocoded"))
            if location:
                exif["location"] = location
            if geocoded:
                exif["geocoded"] = True
            prepared.location = location
            prepared.exif = exif or {}
        status = existing.get("status") if existing else "pending"
        record = base_record(prepared, existing=existing, status=status)
        record["geocoded"] = 1 if geocoded else 0
        if refresh_exif:
            record["location"] = prepared.location
            record["exif"] = prepared.exif
        db.upsert_photo(record)
        return True
    except Exception as exc:  # noqa: BLE001
        _record_photo_error(image_path, folder, db, exc)
        return False


def _raw_owner_map(
    pairs: list[tuple[str, str | None]], current_set: set[str]
) -> dict[str, str]:
    """Map ``raw path -> image path`` for raw files that are not on their own.

    A raw file that now has a same-named image is not a photo any more, so a
    stale raw-only row for it must be folded into the image row instead of
    being reported as a deleted file.
    """
    owners: dict[str, str] = {}
    for image_path, raw_path in pairs:
        if not raw_path or raw_path in current_set:
            continue
        owners[os.path.normcase(canonical_path(raw_path))] = image_path
    return owners


def _attach_new_raw_siblings(
    pairs: list[tuple[str, str | None]],
    db: Database,
    only_paths: list[str] | None = None,
    extra_raw_paths: list[str] | None = None,
) -> int:
    """Reconcile raw/image pairings with what is on disk now.

    Two cases matter, and both are about half a pair arriving late:

    * a ``.nef`` copied in next to an already-indexed ``.jpg`` — the badge has
      to light up, without re-reading EXIF or re-running the model;
    * a ``.jpg`` copied in next to an already-indexed ``.nef`` — the raw row
      was its own photo until now, and must be folded into the image row
      instead of leaving two cards for one picture.

    Only the pairing columns are touched; score, tags and comment survive.

    ``only_paths`` restricts the work to the files a scoped scan was asked
    about, so an import does not rewrite rows elsewhere in the album.
    ``extra_raw_paths`` names rows that must be folded even when the pairing
    itself is unchanged, which is how the rebuild path hands over the raw-only
    rows it deliberately left in place during its delete sweep.
    """
    wanted = None
    if only_paths is not None:
        wanted = {os.path.normcase(canonical_path(p)) for p in only_paths}
    forced = {os.path.normcase(canonical_path(p)) for p in (extra_raw_paths or [])}
    updated = 0
    absorbed = 0
    for image_path, raw_path in pairs:
        if not raw_path:
            continue
        pair_relevant = wanted is None or (
            os.path.normcase(canonical_path(image_path)) in wanted
            or os.path.normcase(canonical_path(raw_path)) in wanted
        )
        if not pair_relevant and os.path.normcase(canonical_path(raw_path)) not in forced:
            continue
        existing = db.get_photo_by_path(image_path)
        if existing is None:
            continue

        # Fold away a leftover raw-only row for the same picture.
        orphan = db.get_photo_by_path(raw_path)
        if orphan and orphan["id"] != existing["id"] and orphan.get("status") != "deleted":
            if not existing.get("score") and orphan.get("score"):
                # The raw row was the one that got analysed; keep its result on
                # the image row rather than discarding a paid-for model call.
                db.adopt_analysis(orphan["id"], existing["id"])
            db.delete_rows([orphan["id"]])
            logger.info("merged raw-only row %s into %s", orphan["path"], image_path)
            absorbed += 1

        if existing.get("raw_path") != raw_path:
            db.update_raw_path(existing["id"], raw_path)
            updated += 1
    if updated or absorbed:
        logger.info(
            "raw pairing reconciled: %d updated, %d merged", updated, absorbed
        )
    return updated + absorbed


def _run_index_phase(
    job_id: str,
    images: list[tuple[str, str | None]],
    folder: str,
    db: Database,
    config: Config,
    refresh_exif: bool = False,
    resolve_location: bool = False,
    processed_offset: int = 0,
) -> bool:
    """Build proxies for a list of ``(image, raw_sibling)`` pairs."""
    JOBS.update(
        job_id,
        phase="index",
        total=processed_offset + len(images),
        processed=processed_offset,
        current="",
    )
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
                raw_path,
            ): image_path
            for image_path, raw_path in images
        }
        for processed, future in enumerate(as_completed(futures), start=1):
            image_path = futures[future]
            JOBS.update(
                job_id, processed=processed_offset + processed, current=image_path
            )
            job = JOBS.get(job_id)
            if not job or job.cancelled:
                JOBS.update(job_id, status="cancelled")
                for pending in futures:
                    pending.cancel()
                return False
    return True


def _scan_worker(
    job_id: str,
    folder: str,
    db: Database,
    config: Config,
    force: bool,
    only_paths: list[str] | None = None,
    analyze: bool = True,
) -> None:
    """Index (and optionally analyse) a folder.

    ``only_paths`` restricts the work to an explicit set of files, which is what
    an import uses so that adding a card's worth of photos never re-queues the
    rest of the library. ``analyze=False`` stops after indexing, so the caller
    can bring photos in without calling the vision model.
    """
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
        pairs = discover_photo_pairs(
            folder,
            config.image_extensions,
            config.raw_extensions,
            skip_dirs=skip_dirs,
        )
        if only_paths is not None:
            # Keep the primary file of each group plus its raw sibling, so a
            # freshly copied .nef still attaches to the .jpg already indexed.
            wanted = {os.path.normcase(canonical_path(p)) for p in only_paths}
            pairs = [
                (image, raw)
                for image, raw in pairs
                if os.path.normcase(canonical_path(image)) in wanted
                or (raw and os.path.normcase(canonical_path(raw)) in wanted)
            ]
            active_paths &= {image for image, _raw in pairs}
        images = [image_path for image_path, _raw in pairs]

        # Index every photo we have not seen yet. This also covers files added
        # after a previous scan, including a forced re-scan.
        if not active_paths:
            to_index = list(pairs)
        else:
            to_index = [
                pair for pair in pairs if pair[0] not in active_paths
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

        # Reconcile pairings once both halves have rows. Doing this after the
        # index phase is what lets a freshly copied .jpg absorb a raw-only row
        # that was indexed earlier, instead of leaving two cards for one photo.
        _attach_new_raw_siblings(pairs, db, only_paths=only_paths)

        if not analyze:
            if not JOBS.get(job_id).cancelled:
                JOBS.update(
                    job_id,
                    phase="index",
                    current="已完成索引（未调用模型）",
                    status="completed",
                )
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
            except (OSError, sqlite3.Error):
                logger.warning("post-scan cleanup failed for %s", folder, exc_info=True)
            JOBS.update(job_id, status="completed")
    except Exception as exc:  # noqa: BLE001
        JOBS.update(job_id, status="error", error=str(exc))


def start_rebuild_index(
    folder: str, db: Database, config: Config, force: bool = False
) -> ScanJob:
    job = JOBS.create(folder, force)
    thread = threading.Thread(
        target=_rebuild_worker,
        args=(job.id, folder, db, config, force),
        daemon=True,
    )
    thread.start()
    return job


def _rebuild_worker(
    job_id: str, folder: str, db: Database, config: Config, force: bool = False
) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    JOBS.update(job_id, status="running", phase="index", error=None)
    try:
        skip_dirs = [config.data_dir, config.trash_dir_name, config.cache_dir_name]
        pairs = discover_photo_pairs(
            folder,
            config.image_extensions,
            config.raw_extensions,
            skip_dirs=skip_dirs,
        )
        images = [image_path for image_path, _raw in pairs]
        current_set = set(images)
        active_paths = set(db.paths_for_folder(folder))
        removed = active_paths - current_set
        added = [pair for pair in pairs if pair[0] not in active_paths]
        # With the "重新整理全部" checkbox enabled, every current photo is
        # refreshed (EXIF/GPS/location) while analysis results are preserved.
        to_index = list(pairs) if force else added
        total_work = len(removed) + len(to_index)
        JOBS.update(
            job_id,
            phase="index",
            total=max(1, total_work),
            processed=0,
            current="",
        )

        # Deleted files: remove their DB row and their proxy cache immediately.
        # A raw-only row is special: when its same-named image has just shown
        # up, the row is not "deleted", it is absorbed by the image row. The
        # image row does not exist yet at this point, so those rows are left
        # alone here and merged after the index phase, which is what lets a
        # model result the raw row carries move across instead of being lost.
        raw_owner = _raw_owner_map(pairs, current_set)
        deferred_merges: list[str] = []
        for processed, path in enumerate(removed, start=1):
            try:
                row = db.get_photo_by_path(path)
                if row:
                    if raw_owner.get(os.path.normcase(canonical_path(path))):
                        deferred_merges.append(path)
                        JOBS.update(job_id, processed=processed, current=path)
                        continue
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
            JOBS.update(job_id, processed=processed, current=path)

        # Already indexed photos are skipped: only newly discovered files are
        # indexed here (proxy + EXIF), without touching existing results.
        if to_index:
            if not _run_index_phase(
                job_id,
                to_index,
                folder,
                db,
                config,
                refresh_exif=True,
                resolve_location=True,
                processed_offset=len(removed),
            ):
                return
        else:
            JOBS.update(
                job_id,
                processed=max(1, total_work),
                current="没有需要更新的照片",
            )

        # Reconcile pairings once both halves have rows: a .nef that just
        # appeared lights up the badge, and a .jpg that just appeared absorbs
        # the raw-only row that used to represent it.
        _attach_new_raw_siblings(pairs, db, extra_raw_paths=deferred_merges)

        if not JOBS.get(job_id).cancelled:
            try:
                cleanup_folder_cache(folder, db, config.cache_dir_name)
            except (OSError, sqlite3.Error):
                logger.warning("post-scan cleanup failed for %s", folder, exc_info=True)
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
            raw_path=photo.get("raw_path"),
            already_geocoded=bool(photo.get("geocoded")),
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
