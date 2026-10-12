"""One-off repairs for records written by earlier versions.

These passes are about *existing* rows rather than about reading new photos:
they fix data an older build wrote with a bug, and they are cheap enough to run
once on startup without the user noticing.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from .config import Config
from .db import Database
from .thumbnailer import image_size_on_disk

logger = logging.getLogger(__name__)

#: Settings key + version, so each repair runs once per database.
ORIENTATION_REPAIR_KEY = "orientation_repair"
ORIENTATION_REPAIR_VERSION = "1"

_REPAIR_LOCK = threading.Lock()
_REPAIR_THREADS: set[threading.Thread] = set()


def register_repair_thread(thread: threading.Thread) -> None:
    with _REPAIR_LOCK:
        _REPAIR_THREADS.add(thread)


def wait_for_repairs(timeout: float | None = None) -> None:
    """Block until running repairs finish.

    Called when the database is closed so a repair cannot keep writing while
    the process (or a test) is tearing the storage down.
    """
    with _REPAIR_LOCK:
        threads = list(_REPAIR_THREADS)
    for thread in threads:
        thread.join(timeout)
    with _REPAIR_LOCK:
        _REPAIR_THREADS.difference_update(threads)


def repair_orientation_sizes(db: Database, config: Config) -> dict[str, int]:
    """Make stored ``width``/``height`` match how the photo actually looks.

    Earlier versions recorded the size straight from the image header, which
    ignores the EXIF orientation tag: a portrait shot was stored as its
    landscape sensor frame. The gallery draws a card from those numbers, so
    those photos came out sideways even though the files themselves are fine.

    Only the header of each file is read (about 0.1 ms per photo), which is what
    makes a whole-library pass affordable. A row whose size is wrong gets two
    fixes:

    * the corrected ``width``/``height``;
    * its rendered cache dropped, because the thumbnail and the proxy on disk
      were drawn from the un-rotated image and would keep the bug visible. They
      are regenerated on demand the next time the photo is shown, which also
      means the model only ever sees upright pictures.

    Raw-only rows go through the same path: their size is measured from the
    embedded preview that actually gets rendered, not from the container header.
    """
    rows = db.rows_for_orientation_check()
    report = {"checked": 0, "fixed": 0, "cache_dropped": 0}

    def measure(row):
        pid, path, width, height, thumb, proxy, _phash = row
        if not path or not os.path.exists(path):
            return None
        display = image_size_on_disk(path)
        if display is None:
            return None
        return (pid, path, width, height, thumb, proxy, display)

    workers = max(1, min(16, int(config.index_concurrency or 4)))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="lumina-fix"
    ) as pool:
        measured = [r for r in pool.map(measure, rows) if r]
    report["checked"] = len(measured)

    size_updates: list[tuple[int, int, int]] = []
    to_drop: list[int] = []
    stale_cache: list[str] = []

    for pid, _path, width, height, thumb, proxy, display in measured:
        if display == (width, height):
            continue
        size_updates.append((pid, display[0], display[1]))
        if thumb or proxy:
            to_drop.append(pid)
            stale_cache.extend(p for p in (thumb, proxy) if p)

    if size_updates:
        db.update_dimensions_only(size_updates)
        report["fixed"] = len(size_updates)

    if to_drop:
        # Clearing the columns is what makes the on-demand generation in
        # /api/thumbnail and /api/proxy run again; deleting the files reclaims
        # the space now instead of leaving it to the next cache cleanup.
        report["cache_dropped"] = db.clear_rendered_cache(to_drop)
        for cached in stale_cache:
            try:
                os.remove(cached)
            except OSError:
                logger.debug("stale cache file is already gone: %s", cached)

    return report


def needs_orientation_repair(db: Database) -> bool:
    """True when this database has not been through the pass yet."""
    return db.get_meta(ORIENTATION_REPAIR_KEY) != ORIENTATION_REPAIR_VERSION


def run_startup_repairs(db: Database, config: Config) -> dict[str, object]:
    """Run every pending one-off repair, once per database."""
    result: dict[str, object] = {}
    if needs_orientation_repair(db):
        report = repair_orientation_sizes(db, config)
        db.set_meta(ORIENTATION_REPAIR_KEY, ORIENTATION_REPAIR_VERSION)
        result["orientation"] = report
        logger.info("orientation repair: %s", report)
    return result
