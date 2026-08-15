"""Per-folder cache helpers.

Only proxy images and UI thumbnails are stored inside the selected photo
folder, under a hidden cache directory (by default ``.photo-review-cache``).
All scan results and scores are stored in the central SQLite database.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .thumbnailer import cleanup_cache

logger = logging.getLogger(__name__)


def cache_dir_for_folder(
    folder: str, cache_dir_name: str = ".photo-review-cache"
) -> Path:
    return Path(folder) / cache_dir_name


def ensure_cache_dirs(folder: str, cache_dir_name: str = ".photo-review-cache") -> Path:
    """Ensure the proxy cache directory exists and return its path."""
    cache_dir = cache_dir_for_folder(folder, cache_dir_name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def cleanup_folder_cache(
    folder: str, db, cache_dir_name: str = ".photo-review-cache"
) -> dict[str, int]:
    """Remove unreferenced proxy/thumbnail files inside one folder's cache.

    Proxy images live directly in the cache folder root. This also removes old
    ``thumbnails/`` subdirectories and legacy JSON cache files.
    """
    cache_dir = cache_dir_for_folder(folder, cache_dir_name)
    referenced = []
    for item in db.all_cache_paths():
        if item.get("thumb_path"):
            referenced.append(item["thumb_path"])
        if item.get("proxy_path"):
            referenced.append(item["proxy_path"])
    result = cleanup_cache(referenced, str(cache_dir))

    # Remove legacy JSON cache files.
    for name in ("scan_results.json", "scan_results.json.tmp"):
        try:
            path = cache_dir / name
            if path.exists():
                path.unlink()
        except OSError:
            logger.debug("legacy cache file is already gone: %s", path)

    # Remove old nested thumbnails directory (previous layout).
    old_thumb_dir = cache_dir / "thumbnails"
    if old_thumb_dir.exists():
        try:
            import shutil

            shutil.rmtree(old_thumb_dir, ignore_errors=True)
        except OSError:
            logger.debug(
                "legacy thumbnails directory is already gone: %s", old_thumb_dir
            )
    return result


def load_scan_results_from_cache(
    folder: str, db, cache_dir_name: str = ".photo-review-cache"
) -> int:
    """Import a previous ``scan_results.json`` cache into the database.

    This makes the UI show earlier scan results immediately when a folder is
    selected, and lets an interrupted scan resume without re-analyzing photos
    that already have cached results.
    """
    cache_dir = cache_dir_for_folder(folder, cache_dir_name)
    cache_file = cache_dir / "scan_results.json"
    if not cache_file.exists():
        return 0

    try:
        with open(cache_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0

    photos = data.get("photos", []) if isinstance(data, dict) else []
    imported = 0
    for item in photos:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not path:
            continue
        existing = db.get_photo_by_path(path)
        if existing:
            # Never resurrect permanently deleted rows or overwrite newer analyzed results.
            if existing.get("status") == "deleted":
                continue
            if (
                existing.get("status") == "analyzed"
                and existing.get("score") is not None
            ):
                continue
        try:
            db.upsert_photo(item)
            imported += 1
        except (TypeError, ValueError, KeyError, sqlite3.Error) as exc:
            logger.warning("could not import legacy cache item %s: %s", path, exc)
            continue

    # This JSON is a legacy cache. After importing into SQLite, remove it so the
    # per-folder directory only keeps proxy/thumbnail images.
    try:
        cache_file.unlink()
    except OSError:
        logger.debug("legacy cache file is already gone: %s", cache_file)
    try:
        (cache_dir / "scan_results.json.tmp").unlink()
    except OSError:
        logger.debug(
            "legacy cache tmp file is already gone: %s",
            cache_dir / "scan_results.json.tmp",
        )
    return imported
