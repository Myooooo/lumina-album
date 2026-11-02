"""Rolling backups of the library database.

The index holds work that cannot be rebuilt cheaply: the model verdict for every
analysed photo (a score, three dimension scores, tags, a comment), the memory
edits the user typed in by hand, favourites, card pairings, and the queued card
actions. Re-running the model over a few thousand photos takes hours and does not
even produce the same answers, so losing the file is losing the library's
*meaning* - the photos themselves are of course untouched.

Nothing protected it. One bad migration, one interrupted write, one mistaken
"remove folder", and it is gone.

The backup is a plain SQLite ``VACUUM INTO``, which writes a consistent snapshot
of a live database without blocking readers - unlike a file copy, which can
capture a torn WAL under an active writer.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from .db import Database

logger = logging.getLogger(__name__)

#: How many dated backups to keep. Small enough not to fill a disk, large enough
#: to survive noticing a problem a few starts later.
BACKUP_KEEP = 5

#: Minimum seconds between automatic backups. The app is usually restarted while
#: being worked on, and a backup per restart would churn for no benefit.
BACKUP_MIN_INTERVAL = 6 * 3600

#: Never back up a database smaller than this: an empty or just-created file has
#: nothing worth keeping and would push real backups out of the rotation.
BACKUP_MIN_BYTES = 64 * 1024

#: Settings key remembering when the last automatic backup happened.
BACKUP_STAMP_KEY = "last_auto_backup"


def backup_dir_for(db_path: str) -> Path:
    """Where automatic backups live, next to the database."""
    return Path(db_path).resolve().parent / "backups"


def backup_database(db_path: str, keep: int = BACKUP_KEEP) -> Path | None:
    """Write a snapshot of ``db_path`` and prune old ones.

    Returns the new file, or ``None`` when there was nothing to copy or the copy
    failed. A failed backup must never stop the application from starting, so
    every error is logged and swallowed.
    """
    source = Path(db_path)
    if not source.exists():
        return None
    try:
        if source.stat().st_size < BACKUP_MIN_BYTES:
            return None
    except OSError:
        return None

    target_dir = backup_dir_for(db_path)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("could not create the backup directory", exc_info=True)
        return None

    # Milliseconds in the name: two backups inside the same second would
    # otherwise share a filename and the second would silently replace the first,
    # which made the rotation look like it was not keeping anything.
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    target = target_dir / f"library-{stamp}.db"
    temp = target.with_suffix(".db.partial")
    try:
        temp.unlink(missing_ok=True)
        # ``VACUUM INTO`` needs a path that does not exist yet, and produces a
        # fully consistent copy even while another thread is writing - which a
        # plain file copy cannot do under an active WAL.
        with sqlite3.connect(str(source), timeout=30) as handle:
            handle.execute("VACUUM INTO ?", (str(temp),))
        os.replace(temp, target)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("database backup failed: %s", exc)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    _prune_backups(target_dir, keep)
    return target


def _prune_backups(directory: Path, keep: int) -> None:
    """Keep the newest ``keep`` snapshots."""
    try:
        files = sorted(
            (p for p in directory.iterdir() if p.name.startswith("library-")),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            logger.debug("could not remove old backup %s", stale)


def maybe_auto_backup(db: Database, db_path: str) -> Path | None:
    """Back up at most once per ``BACKUP_MIN_INTERVAL``, tracked in app_meta."""
    now = time.time()
    try:
        last = float(db.get_meta(BACKUP_STAMP_KEY) or 0)
    except (TypeError, ValueError):
        last = 0.0
    if now - last < BACKUP_MIN_INTERVAL:
        return None
    target = backup_database(db_path)
    if target is not None:
        db.set_meta(BACKUP_STAMP_KEY, str(int(now)))
        logger.info("database backed up to %s", target)
    return target


def free_space_note(directory: Path) -> str:
    """Human-readable free space, for the settings dialog."""
    try:
        usage = shutil.disk_usage(directory)
    except OSError:
        return ""
    return f"{usage.free / 1073741824:.1f} GB"
