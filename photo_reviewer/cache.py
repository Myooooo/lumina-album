"""Per-folder cache helpers.

Only proxy images and UI thumbnails are stored inside the selected photo
folder, under a hidden cache directory (by default ``.photo-review-cache``).
All scan results and scores are stored in the central SQLite database.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .thumbnailer import cleanup_cache

logger = logging.getLogger(__name__)


def cache_dir_for_folder(
    folder: str, cache_dir_name: str = ".photo-review-cache"
) -> Path:
    """Return the cache directory path for one photo folder.

    The name is validated because everything below this point deletes files
    inside the returned directory: an empty or relative name such as ``""`` or
    ``"."`` would resolve to the photo folder itself and turn cache cleanup into
    photo deletion. A hand-edited ``PHOTO_CACHE_DIR_NAME`` is the only way to get
    such a value in, so it is refused here rather than trusted.
    """
    name = str(cache_dir_name or "").strip()
    if not name or name in {".", ".."} or os.path.basename(name) != name:
        raise ValueError(f"缓存目录名不合法: {cache_dir_name!r}")
    return Path(folder) / name


def ensure_cache_dirs(folder: str, cache_dir_name: str = ".photo-review-cache") -> Path:
    """Ensure the proxy cache directory exists and return its path."""
    cache_dir = cache_dir_for_folder(folder, cache_dir_name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


#: Every column that points at a file the app rendered for a row. A preview, a
#: play copy and a Live Photo's motion part all live in the cache directory and
#: all have to be swept when the row goes; listing them once is what keeps the
#: sweep and the per-row delete from drifting apart when a new kind is added.
DERIVED_PATH_FIELDS = (
    "thumb_path",
    "proxy_path",
    "clip_path",
    "play_path",
    "live_path",
)


def cleanup_folder_cache(
    folder: str, db, cache_dir_name: str = ".photo-review-cache"
) -> dict[str, int]:
    """Remove unreferenced proxy/thumbnail files inside one folder's cache.

    Proxy images live directly in the cache folder root; anything without a
    matching row in the database is a leftover from a deleted photo or an
    interrupted write, so it is swept here.
    """
    cache_dir = cache_dir_for_folder(folder, cache_dir_name)
    referenced = []
    for item in db.cache_paths_for_folder(folder):
        for key in DERIVED_PATH_FIELDS:
            if item.get(key):
                referenced.append(item[key])
    return cleanup_cache(referenced, str(cache_dir))
