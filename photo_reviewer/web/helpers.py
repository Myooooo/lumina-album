"""Request-independent helpers for the HTTP layer.

Payload shaping, trash moves, card synchronisation and the startup work live
here so the routes stay about routing. Only the cache-header helper reads the
current request, which is why this module imports ``request`` and nothing else
from Flask.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import request

from .. import video
from ..api_client import LEGACY_DIMENSION_KEYS, strip_json_format
from ..backup import backup_due
from ..cache import (
    DERIVED_PATH_FIELDS,
    cache_dir_for_folder,
    cleanup_folder_cache,
    ensure_cache_dirs,
)
from ..cards import (
    CardError,
    delete_from_card,
    purge_for_photo,
    relative_to_album,
    restore_to_card,
    target_for,
)
from ..config import PERSISTED_FIELDS, Config
from ..db import Database
from ..exif import extract_exif
from ..geocode import geocoding_enabled, reverse_geocode
from ..paths import canonical_folder, canonical_path, is_in_named_dir, same_path
from ..repair import register_repair_thread, repairs_pending, run_startup_repairs
from ..scanner import JOBS, RAW_EXTENSIONS
from .constants import _IMMUTABLE_IMAGE_MAX_AGE

logger = logging.getLogger(__name__)


def _duplicate_payload(photo: dict[str, Any]) -> dict[str, Any]:
    """A review row: enough to recognise the photo and compare the copies.

    No EXIF or tags - the decision here is made by looking, and the only numbers
    that matter are the ones that tell you which copy to keep (resolution and file
    size), plus the thumbnail version so the browser can cache it.
    """
    raw_path = photo.get("raw_path")
    return {
        "id": photo["id"],
        "filename": photo.get("filename") or "",
        "path": photo.get("path") or "",
        "folder": photo.get("folder") or "",
        "width": int(photo.get("width") or 0),
        "height": int(photo.get("height") or 0),
        "size": int(photo.get("size") or 0),
        "media_kind": photo.get("media_kind") or "photo",
        "status": photo.get("status") or "",
        "score": photo.get("score"),
        "favorite": bool(photo.get("favorite")),
        # The dialog renders a RAW marker and a path row from these four; without
        # them a photo opened from this view looked location-less and raw-less.
        "raw_path": raw_path,
        "has_raw": bool(raw_path),
        "is_raw": _is_raw_file(photo.get("path") or photo.get("original_path")),
        "raw_ext": Path(raw_path).suffix.lstrip(".").upper() if raw_path else "",
        # The same cache-busting token the gallery uses: the dHash changes when
        # the thumbnail is re-rendered, so the browser can keep this one forever.
        "thumb_version": photo.get("phash") or "",
    }


def _photo_payload(photo: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    """Shape one photo row for the API.

    The gallery only needs what it renders; paths, hashes, the model name and
    the EXIF block are dropped from list responses. ``detail=True`` keeps them
    for single-photo endpoints.

    Both the EXIF block and the raw sibling's size were once sent with every
    list row, which cost two things on a 4000-photo album: ~30% of the payload
    was EXIF that no card reads, and every photo with a raw file triggered an
    ``os.path.getsize`` on the album drive - on an external disk, a page of 100
    could stall for a second. The detail dialog fetches both on demand from
    ``/api/photo/<id>/detail``, one small request per photo actually opened.

    ``raw_path`` is set when a same-named camera-raw file sits next to the
    image; ``is_raw`` is set when the photo itself is the raw file because no
    JPEG counterpart exists.
    """
    raw_path = photo.get("raw_path")
    # A trashed pair keeps saying it has a raw half, so the permanent-delete
    # dialog can offer to remove both. Rows trashed by an older build cleared
    # that column; for those the file is looked for beside the image, and only
    # for deleted rows - this is the one place the list may touch the disk, and
    # the trash view is small.
    if not raw_path and photo.get("status") == "deleted":
        raw_path = _trashed_raw_candidate(photo)
    payload = {
        "id": photo["id"],
        "path": photo["path"],
        "filename": photo["filename"],
        "size": photo["size"],
        "width": photo["width"],
        "height": photo["height"],
        "location": photo.get("location"),
        "dimensions": _clean_dimensions(photo.get("dimensions")),
        "score": photo.get("score"),
        "tags": photo.get("tags") or [],
        "reason": photo.get("reason"),
        "title": photo.get("title") or "",
        "analyzed_at": photo.get("analyzed_at"),
        "status": photo.get("status"),
        "favorite": bool(photo.get("favorite")),
        "error": photo.get("error"),
        "raw_path": raw_path,
        "has_raw": bool(raw_path),
        "is_raw": _is_raw_file(photo.get("path") or photo.get("original_path")),
        "raw_ext": Path(raw_path).suffix.lstrip(".").upper() if raw_path else "",
        # The thumbnail URL carries this, so the browser can cache the image
        # forever: re-rendering a thumbnail (cache clean, orientation repair)
        # changes the hash and therefore the URL. A 16-char string is far
        # cheaper than revalidating thousands of images.
        "thumb_version": photo.get("phash") or "",
        # A video card is drawn differently from a photo card, and its duration
        # is on the card itself, so both travel with the row.
        "media_kind": photo.get("media_kind") or "photo",
        "duration_ms": photo.get("duration_ms"),
        #: True when the still carries a short video (motion photo / Live Photo).
        "has_live": bool(photo.get("live_path")),
    }
    if detail:
        payload.update(
            {
                "original_path": photo.get("original_path"),
                "folder": photo["folder"],
                "thumb_path": photo.get("thumb_path"),
                "proxy_path": photo.get("proxy_path"),
                "phash": photo.get("phash"),
                "model": photo.get("model"),
                "geocoded": bool(photo.get("geocoded")),
                "exif": photo.get("exif") or {},
                # Size of the raw sibling, so the detail page can say
                # "10 MB + 30 MB". One stat() call, only for the photo on
                # screen - never for a whole page.
                "raw_size": _file_size(raw_path),
            }
        )
    return payload


def _clean_dimensions(raw: Any) -> dict[str, Any]:
    """Drop retired dimension keys from a stored object before it is served.

    The startup migration rewrites every row, but a row can be read before that
    background pass reaches it (or after a hand-edited database). The retired
    key is merged into "technical" with the same rule the migration and the
    analysis path use, so every caller sees one consistent shape.
    """
    if not isinstance(raw, dict) or not raw:
        return raw if isinstance(raw, dict) else {}
    retired = [key for key in LEGACY_DIMENSION_KEYS if key in raw]
    if not retired:
        return raw
    merged = dict(raw)
    values = []
    for key in retired:
        try:
            values.append(max(0.0, min(10.0, float(merged.pop(key) or 0))))
        except (TypeError, ValueError):
            values.append(0.0)
    try:
        technical = max(0.0, min(10.0, float(merged.get("technical") or 0)))
    except (TypeError, ValueError):
        technical = 0.0
    merged["technical"] = round((technical + sum(values)) / (len(values) + 1), 2)
    return merged


def _file_size(path: str | None) -> int:
    """Size of a file on disk, or 0 when it is missing.

    Used for the raw sibling's size in the detail payload. Only single-photo
    endpoints pay for this, so the gallery list never stats files it does not
    display.
    """
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _normalize_folder(path: str) -> str:
    """Resolve a user-supplied folder to its on-disk spelling."""
    return canonical_folder(path)


#: ui_state key holding the albums the main path selectors must not offer.
HIDDEN_FOLDERS_KEY = "hidden_folders"


def hidden_folder_list(database: Database) -> list[str]:
    """Albums the reader hid from the main path selectors, newest first."""
    raw = database.get_ui_state().get(HIDDEN_FOLDERS_KEY)
    if not isinstance(raw, list):
        return []
    folders: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        resolved = canonical_path(text)
        key = os.path.normcase(resolved)
        if not resolved or key in seen:
            continue
        seen.add(key)
        folders.append(resolved)
    return folders


def is_hidden_folder(database: Database, folder: str | None) -> bool:
    """True when ``folder`` is one of the hidden albums."""
    if not folder:
        return False
    key = os.path.normcase(canonical_path(folder))
    return any(
        os.path.normcase(item) == key for item in hidden_folder_list(database)
    )


def set_folder_hidden(
    database: Database, folder: str, hidden: bool
) -> list[str]:
    """Hide or unhide one album, returning the new hidden list (newest first)."""
    resolved = canonical_path(folder)
    if not resolved:
        raise ValueError("文件夹路径不合法")
    key = os.path.normcase(resolved)
    kept = [
        item for item in hidden_folder_list(database) if os.path.normcase(item) != key
    ]
    if hidden:
        kept.insert(0, resolved)
    database.save_ui_state({HIDDEN_FOLDERS_KEY: kept})
    return kept


def drop_folder_from_ui_state(database: Database, folder: str) -> None:
    """Forget ``folder`` as the remembered album and in the history.

    Called when an album is hidden: the next page load must land on the last
    *visible* album, and the history dropdown must not offer the hidden one.
    """
    key = os.path.normcase(canonical_path(folder))
    state = database.get_ui_state()
    patch: dict[str, Any] = {}
    remembered = state.get("folder")
    if isinstance(remembered, str) and os.path.normcase(canonical_path(remembered)) == key:
        patch["folder"] = ""
    history = state.get("folder_history")
    if isinstance(history, list):
        kept = [
            item
            for item in history
            if os.path.normcase(canonical_path(str(item))) != key
        ]
        if len(kept) != len(history):
            patch["folder_history"] = kept
    if patch:
        database.save_ui_state(patch)


def _as_bool(value: Any, default: bool = False) -> bool:
    """Read a checkbox value that may arrive as a real bool or a string."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _is_raw_file(path: str | None) -> bool:
    return bool(path) and Path(path).suffix.lower() in RAW_EXTENSIONS


def _open_with_system(path: str) -> None:
    """Hand a path to the operating system's default handler."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _thumbnail_path(photo: dict[str, Any]) -> str | None:
    """Return an existing gallery thumbnail for ``photo``, if there is one.

    Older databases stored the proxy path in ``thumb_path``. Rather than
    trusting the column blindly, the sibling ``*_thumb.jpg`` next to the proxy
    is checked as well, which is what the thumbnailer actually writes.
    """
    thumb = photo.get("thumb_path")
    candidates = []
    if thumb:
        candidates.append(thumb)
        if thumb.endswith("_proxy.jpg"):
            candidates.append(f"{thumb[: -len('_proxy.jpg')]}_thumb.jpg")
        elif not thumb.endswith("_thumb.jpg"):
            stem, _, _ = thumb.rpartition(".")
            candidates.append(f"{stem}_thumb.jpg" if stem else f"{thumb}_thumb.jpg")
    proxy = photo.get("proxy_path")
    if proxy and proxy.endswith("_proxy.jpg"):
        candidates.append(f"{proxy[: -len('_proxy.jpg')]}_thumb.jpg")
    for candidate in candidates:
        if candidate.endswith("_thumb.jpg") and os.path.exists(candidate):
            return candidate
    return None


def _image_cache_timeout() -> int | None:
    """How long a thumbnail may be cached, given the request.

    ``?v=<hash>`` means the URL is content-addressed by the caller, so the
    response can be cached for a year - that is what makes scrolling a long
    album back and forth free. A bare ``/api/thumbnail/<id>`` is not
    content-addressed (the file behind it can be re-rendered at any time), so it
    keeps Flask's default of ``no-cache`` and revalidates with its ETag.
    """
    return _IMMUTABLE_IMAGE_MAX_AGE if request.args.get("v") else None


#: ``/api/years`` parses the EXIF JSON of every row in the folder, which is why
#: it cost ~22 ms on an 11k-photo library while the rest of the panel answered in
#: single digits. The answer only changes when a scan does, so it is memoised for
#: a short while; the key includes the folder so switching albums is unaffected.
YEARS_CACHE_SECONDS = 60.0
_years_cache: dict[str, tuple[float, list[int]]] = {}


def _cached_years(database: Database, folder: str | None) -> list[int]:
    """Distinct capture years, memoised for ``YEARS_CACHE_SECONDS``."""
    key = folder or ""
    now = time.monotonic()
    hit = _years_cache.get(key)
    if hit and now - hit[0] < YEARS_CACHE_SECONDS:
        return hit[1]
    years = database.capture_years(folder)
    if len(_years_cache) > 32:
        _years_cache.clear()
    _years_cache[key] = (now, years)
    return years


def invalidate_years_cache() -> None:
    """Drop the memoised years, e.g. after a scan indexed new photos."""
    _years_cache.clear()


def _trash_destination(day_dir: Path, source: str) -> Path:
    """Pick a free name inside the day folder, avoiding collisions."""
    dest = day_dir / Path(source).name
    if dest.exists():
        dest = (
            day_dir
            / f"{Path(source).stem}_{uuid.uuid4().hex[:8]}{Path(source).suffix.lower()}"
        )
    return dest


def _move_to_trash(
    photo: dict[str, Any], config: Config, move_raw: bool = True
) -> tuple[str, str | None]:
    """Move a photo into a trash directory under its folder.

    When the photo has a same-named raw sibling, the raw file is moved with it
    so the pair never gets split across the album and the trash.

    Returns ``(image_path_in_trash, raw_path_in_trash)``. The raw location is
    kept so the row can go on saying "this pair has a raw half": without it the
    permanent-delete dialog had no way to know the pair was still a pair, and
    could not offer to delete both.
    """
    src = photo["path"]
    if not os.path.exists(src):
        raise FileNotFoundError(f"文件不存在: {src}")

    folder = photo["folder"] or os.path.dirname(src)
    trash_root = Path(folder) / config.trash_dir_name
    day_dir = trash_root / datetime.now().astimezone().strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    trashed_raw: str | None = None
    if move_raw:
        raw_path = photo.get("raw_path")
        if raw_path and os.path.exists(raw_path) and not same_path(raw_path, src):
            try:
                raw_dest = _trash_destination(day_dir, raw_path)
                shutil.move(raw_path, str(raw_dest))
                trashed_raw = str(raw_dest)
            except OSError:
                logger.warning(
                    "could not move raw sibling %s to trash", raw_path, exc_info=True
                )

    # A merged Live Photo is one memory made of two files: the movie goes to the
    # trash with the still, or the next scan would list it as a new video card.
    live_path = _live_sibling(photo, config)
    if live_path and not same_path(live_path, src):
        try:
            shutil.move(live_path, str(_trash_destination(day_dir, live_path)))
        except OSError:
            logger.warning(
                "could not move the live photo's movie %s to trash",
                live_path,
                exc_info=True,
            )

    dest = _trash_destination(day_dir, src)
    shutil.move(src, str(dest))
    return str(dest), trashed_raw


def _restore_from_trash(
    photo: dict[str, Any], restore_raw: bool = True, config: Config | None = None
) -> tuple[str, str | None]:
    """Move a deleted photo from trash back to its original path.

    A raw sibling that was trashed alongside the photo is put back as well, so
    the pair is reunited in the album. A Live Photo's movie comes back too - it
    was moved with the still, and leaving it in the trash would break the pair.
    Returns ``(image_path, raw_path)``.
    """
    trash_path = photo["path"]
    original = photo.get("original_path") or trash_path
    if not os.path.exists(trash_path):
        raise FileNotFoundError(f"回收站文件不存在: {trash_path}")

    original = os.path.abspath(original)
    Path(original).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(original):
        original = _uniquify(original)

    restored_raw: str | None = None
    if restore_raw:
        raw_trashed = _trashed_raw_candidate(photo)
        if raw_trashed:
            raw_original = _original_path_for_trashed(photo, raw_trashed)
            try:
                Path(raw_original).parent.mkdir(parents=True, exist_ok=True)
                if os.path.exists(raw_original):
                    raw_original = _uniquify(raw_original)
                shutil.move(raw_trashed, raw_original)
                restored_raw = raw_original
            except OSError:
                logger.warning(
                    "could not restore raw sibling %s", raw_trashed, exc_info=True
                )

    # The movie a Live Photo carried was moved to the trash alongside the still;
    # bring it back to the same folder so the pair is whole again. A motion photo
    # keeps its video inside the image, so there is nothing to look for.
    if config is not None:
        live_trashed = _trashed_live_candidate(photo, config)
        if live_trashed:
            original_parent = Path(original).parent
            live_original = str(original_parent / Path(live_trashed).name)
            try:
                if os.path.exists(live_original):
                    live_original = _uniquify(live_original)
                shutil.move(live_trashed, live_original)
            except OSError:
                logger.warning(
                    "could not restore the live photo's movie %s",
                    live_trashed,
                    exc_info=True,
                )

    shutil.move(trash_path, original)
    return original, restored_raw


def _uniquify(path: str) -> str:
    stem = Path(path).stem
    suffix = Path(path).suffix
    return str(Path(path).parent / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}")


def _trashed_raw_candidate(photo: dict[str, Any]) -> str | None:
    """Find the raw file that was moved to trash together with ``photo``.

    A row trashed by this version records the raw's location, so it is returned
    directly. Rows trashed by an older one cleared that column, so they are
    located by scanning the photo's trash day folder for a raw file sharing the
    image's file name stem. Files from different source folders land in
    different day folders, so a stem match is unambiguous in practice.
    """
    recorded = photo.get("raw_path")
    if recorded and os.path.exists(recorded) and _is_raw_file(recorded):
        return recorded

    trashed_image = photo.get("path") or ""
    if not trashed_image:
        return None
    day_dir = os.path.dirname(trashed_image)
    if not day_dir or not os.path.isdir(day_dir):
        return None
    stem = Path(trashed_image).stem
    image_suffix = Path(trashed_image).suffix.lower()
    for name in sorted(os.listdir(day_dir)):
        candidate = os.path.join(day_dir, name)
        if not os.path.isfile(candidate):
            continue
        if Path(name).suffix.lower() == image_suffix:
            continue
        if Path(name).stem != stem:
            continue
        if _is_raw_file(candidate):
            return candidate
    return None


def _live_sibling(photo: dict[str, Any], config: Config) -> str | None:
    """The movie that belongs to a Live Photo, when it is an original file.

    A still and its movie are one memory: an iPhone writes ``IMG_1234.HEIC``
    next to ``IMG_1234.MOV`` and the pair is shown as a single card. Moving only
    the still to the trash would leave the movie behind, where the next scan
    would find it alone and list it as a brand-new video card.

    Returns ``None`` for a motion photo whose video is inside the image (there is
    nothing separate to move) and for a ``live_path`` inside the app's own cache,
    which is derived data rather than something the user owns.
    """
    live_path = photo.get("live_path")
    if not live_path or not os.path.exists(live_path):
        return None
    folder = photo.get("folder") or os.path.dirname(photo.get("path") or "")
    try:
        cache_root = os.path.abspath(os.path.join(folder, config.cache_dir_name))
        if os.path.abspath(live_path).startswith(cache_root + os.sep):
            return None
    except (TypeError, ValueError):
        return None
    return live_path


def _trashed_live_candidate(photo: dict[str, Any], config: Config) -> str | None:
    """Find the movie that was moved to the trash together with a Live Photo.

    Located by stem inside the photo's trash day folder, the same way the raw
    sibling is found: the pair shares a file name, and different source folders
    land in different day folders.
    """
    trashed_image = photo.get("path") or ""
    if not trashed_image:
        return None
    day_dir = os.path.dirname(trashed_image)
    if not day_dir or not os.path.isdir(day_dir):
        return None
    stem = Path(trashed_image).stem
    image_suffix = Path(trashed_image).suffix.lower()
    for name in sorted(os.listdir(day_dir)):
        candidate = os.path.join(day_dir, name)
        if not os.path.isfile(candidate):
            continue
        if Path(name).stem != stem:
            continue
        if Path(name).suffix.lower() == image_suffix:
            continue
        if video.looks_like_video(candidate):
            return candidate
    return None


def _original_path_for_trashed(photo: dict[str, Any], trashed_raw: str) -> str:
    """Rebuild where a trashed raw file came from."""
    raw_path = photo.get("raw_path") or ""
    original_image = photo.get("original_path") or photo.get("path") or ""
    if raw_path and original_image:
        return str(Path(original_image).parent / Path(raw_path).name)
    if original_image:
        return str(Path(original_image).parent / Path(trashed_raw).name)
    return trashed_raw


def _remove_photo_cache(photo: dict[str, Any], config: Config | None = None) -> None:
    """Delete every cached file a row produced, if present.

    Covers the video derivatives as well: a preview or a transcoded copy left
    behind for a row that no longer exists is invisible to the user and can be
    hundreds of megabytes. Each path is checked against the album's cache
    directory first, so a stale column can never point this at an original.
    """
    folder = photo.get("folder") or ""
    for key in DERIVED_PATH_FIELDS:
        path = photo.get(key)
        if not path or not os.path.exists(path):
            continue
        if config is not None and folder and not _inside_cache_dir(path, folder, config):
            logger.debug("refusing to delete %s: outside the cache directory", path)
            continue
        try:
            _remove_file(path)
        except OSError:
            logger.debug("photo cache file is already gone: %s", path)


def _inside_cache_dir(path: str, folder: str, config: Config) -> bool:
    """True when ``path`` sits inside one folder's cache directory."""
    try:
        root = os.path.abspath(os.path.join(folder, config.cache_dir_name))
        target = os.path.abspath(path)
    except (TypeError, ValueError):
        return False
    return target == root or target.startswith(root + os.sep)


#: How long to keep trying when Windows says a file is busy. Antivirus, the
#: search indexer and Explorer's preview handler all open files briefly, and a
#: delete that lands in that window fails with WinError 32 rather than because
#: anything is actually wrong.
_REMOVE_ATTEMPTS = 6
_REMOVE_BACKOFF = 0.12


def _remove_file(path: str) -> None:
    """Delete ``path``, retrying briefly while another process holds it open.

    Windows refuses to unlink an open file. A thumbnail sweep can hit that on a
    handful of files out of thousands, and without a retry those photos stay in
    the trash with an error the user can do nothing about - the lock is gone a
    fraction of a second later. Anything other than "in use" fails immediately,
    and the last attempt's error is what the caller reports.
    """
    delay = _REMOVE_BACKOFF
    for attempt in range(_REMOVE_ATTEMPTS):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            # Someone else removed it first; the goal is met either way.
            return
        except PermissionError:
            if attempt == _REMOVE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2


def _trash_only(path: str, cfg: Config) -> bool:
    """True when ``path`` sits inside one of the app's own trash directories.

    Nothing outside a trash directory is ever erased outright. The two routes
    that permanently remove files ("彻底移除" and "清空回收站") only ever act on
    photos the user already moved to the trash, so the file they are about to
    delete must live under a ``trash_dir_name`` directory. Checking that here
    means a stale row - one whose ``path`` or ``original_path`` still points at a
    live photo because of an interrupted move - is refused instead of deleting an
    original the user never confirmed.
    """
    if not path:
        return False
    if not os.path.exists(path):
        # Already gone: there is nothing to protect, and the row is still
        # removed by the caller.
        return True
    return is_in_named_dir(path, cfg.trash_dir_name)


def _empty_trash(
    database: Database,
    cfg: Config,
    folder: str | None,
    delete_raw: bool,
    on_progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Erase every trashed photo, returning the counts.

    Split out of the route so the background job and the synchronous call share
    one implementation - in particular the ``_trash_only`` guard, which must not
    be duplicated. ``on_progress(done, total, name)`` is called before each photo
    so the toolbar can show how far along the sweep is; ``should_cancel`` is
    polled between photos, so the toolbar's cancel button works here too.
    """
    photos = database.list_photos(
        folder=folder,
        status="deleted",
        exclude_deleted=False,
        sort="filename",
        limit=100000,
    )
    total = len(photos)
    deleted = 0
    raw_deleted = 0
    card_removed = 0
    card_deferred = 0
    cancelled = False
    errors = []
    touched_folders = set()
    for index, photo in enumerate(photos, start=1):
        if should_cancel and should_cancel():
            cancelled = True
            break
        if on_progress:
            on_progress(index - 1, total, photo.get("filename") or "")
        candidates = []
        if photo.get("path"):
            candidates.append(photo["path"])
        original = photo.get("original_path")
        if original and original not in candidates:
            candidates.append(original)
        if delete_raw:
            raw_candidate = _trashed_raw_candidate(photo)
            if raw_candidate:
                candidates.append(raw_candidate)
        failed = False
        for candidate in candidates:
            if not candidate or not os.path.exists(candidate):
                continue
            # Only files the user already parked in the trash may be erased;
            # anything else means the row is stale and the original is still
            # live, which emptying the bin must never touch.
            if not _trash_only(candidate, cfg):
                errors.append(
                    {
                        "id": photo["id"],
                        "error": "该记录指向的不是回收站文件，已跳过以免误删原图",
                    }
                )
                failed = True
                break
            try:
                _remove_file(candidate)
                if delete_raw and _is_raw_file(candidate):
                    raw_deleted += 1
            except OSError as exc:
                errors.append({"id": photo["id"], "error": f"删除文件失败: {exc}"})
                failed = True
                break
        if failed:
            continue
        # Whatever copy of this photo the card is still holding goes too.
        try:
            card_result = purge_for_photo(
                database, target_for(database, photo.get("folder") or ""), photo["id"]
            )
            card_removed += card_result["purged"]
            card_deferred += card_result["deferred"]
        except (CardError, OSError):
            logger.warning("card purge failed for photo %s", photo["id"], exc_info=True)
        _remove_photo_cache(photo, cfg)
        database.delete_rows([photo["id"]])
        deleted += 1
        touched_folders.add(
            photo.get("folder") or os.path.dirname(photo.get("path") or "")
        )
    for touched in touched_folders:
        try:
            cleanup_folder_cache(touched, database, cfg.cache_dir_name)
        except (OSError, sqlite3.Error):
            logger.warning("cache cleanup failed for %s", touched, exc_info=True)
    if on_progress:
        on_progress(deleted if cancelled else total, total, "")
    return {
        "deleted": deleted,
        "raw_deleted": raw_deleted,
        "card_removed": card_removed,
        "card_deferred": card_deferred,
        "errors": errors,
        "total": total,
        "cancelled": cancelled,
    }


def _empty_trash_worker(
    job_id: str,
    database: Database,
    cfg: Config,
    folder: str | None,
    delete_raw: bool,
) -> None:
    """Run ``_empty_trash`` as a tracked job so the UI can show progress."""
    job = JOBS.get(job_id)
    if not job:
        return
    try:
        # The route already counted the photos, so the bar is honest from the
        # first tick instead of jumping once the listing returns.
        JOBS.update(job_id, status="running", phase="trash", processed=0)
        result = _empty_trash(
            database,
            cfg,
            folder,
            delete_raw,
            on_progress=lambda done, _total, name: JOBS.update(
                job_id, processed=done, current=name
            ),
            should_cancel=lambda: bool(
                JOBS.get(job_id) and JOBS.get(job_id).cancelled
            ),
        )
        if result["cancelled"]:
            JOBS.update(job_id, status="cancelled", report=result)
            return
        JOBS.update(
            job_id,
            status="completed",
            processed=result["total"],
            current="回收站已清空",
            report=result,
        )
    except Exception as exc:
        logger.warning("emptying the trash failed", exc_info=True)
        JOBS.update(job_id, status="error", error=str(exc))


def _card_files(photo: dict[str, Any], raw_path: str | None) -> list[dict[str, Any]]:
    """Describe the local files of a photo as card-relative paths.

    The import mirrors the card's layout, so a file's path relative to the album
    is also its path on the card. Only files that really sat on the card are
    returned: a photo indexed from somewhere else, or stored outside its album
    folder, has no card counterpart.
    """
    album = photo.get("folder") or ""
    if not album:
        return []
    entries = []
    for path, is_raw in ((photo.get("path"), False), (raw_path, True)):
        relative = relative_to_album(album, path)
        if not relative:
            continue
        try:
            size = os.path.getsize(path) if path and os.path.exists(path) else 0
        except OSError:
            size = 0
        entries.append(
            {
                "relative_path": relative,
                "filename": os.path.basename(path),
                "size": size,
                "is_raw": is_raw,
            }
        )
    return entries


def _sync_delete_to_card(
    database: Database,
    cfg: Config,
    photo: dict[str, Any],
    raw_path: str | None,
    sync_card: bool,
) -> dict[str, Any] | None:
    """Handle the card side of trashing a photo.

    ``sync_card`` is the user's answer to the "also delete from the SD card"
    checkbox. When the card is not usable the answer is necessarily false (the
    dialog only offers the checkbox for a card that is plugged in), and the
    deletion is queued instead of being lost, so it can be carried out the next
    time that card appears.
    """
    target = target_for(database, photo.get("folder") or "")
    if target is None:
        return None
    entries = _card_files(photo, raw_path)
    if not entries:
        return None

    result = {"source": target.source, "synced": 0, "queued": 0, "skipped": 0, "errors": []}
    for entry in entries:
        try:
            if target.usable and not sync_card:
                # The card is right here and the user cleared the checkbox, so
                # its copy stays. Nothing is queued either: this was a decision,
                # not a card that happened to be missing.
                result["skipped"] += 1
                continue
            outcome = delete_from_card(
                database,
                target,
                entry["relative_path"],
                cfg.trash_dir_name,
                filename=entry["filename"],
                size=entry["size"],
                is_raw=entry["is_raw"],
                photo_id=photo["id"],
            )
        except (CardError, OSError) as exc:
            logger.warning("card delete failed for %s", entry["relative_path"], exc_info=True)
            result["errors"].append({"path": entry["relative_path"], "error": str(exc)})
            continue
        if outcome.get("synced"):
            result["synced"] += 1
        elif outcome.get("queued"):
            result["queued"] += 1
        else:
            result["skipped"] += 1
    return result


def _sync_restore_to_card(
    database: Database,
    photo: dict[str, Any],
    raw_path: str | None,
    sync_card: bool,
) -> dict[str, Any] | None:
    """Handle the card side of bringing a photo back out of the trash.

    Restoring only undoes a deletion the card actually saw. A card that is away
    keeps a queued restore, which ``apply_pending`` carries out later.
    """
    target = target_for(database, photo.get("folder") or "")
    if target is None:
        return None
    album = photo.get("folder") or ""
    candidates = [
        (photo.get("original_path") or photo.get("path"), False),
        (raw_path, True),
    ]
    result = {"source": target.source, "restored": 0, "queued": 0, "errors": []}
    for path, is_raw in candidates:
        relative = relative_to_album(album, path)
        if not relative:
            continue
        try:
            if target.usable and not sync_card:
                continue
            outcome = restore_to_card(
                database,
                target,
                relative,
                filename=os.path.basename(path or ""),
                is_raw=is_raw,
                photo_id=photo["id"],
            )
        except (CardError, OSError) as exc:
            logger.warning("card restore failed for %s", relative, exc_info=True)
            result["errors"].append({"path": relative, "error": str(exc)})
            continue
        if outcome.get("synced"):
            result["restored"] += 1
        elif outcome.get("queued"):
            result["queued"] += 1
    return result


def _ensure_exif(
    photo: dict[str, Any],
    database: Database,
    cfg: Config | None = None,
    resolve_location: bool = False,
) -> dict[str, Any]:
    """Backfill missing EXIF/location data for photos indexed by older versions.

    Only used by single-photo endpoints: the gallery list never inspects the
    filesystem, because a sleeping external drive would add seconds of latency
    to every page. Location resolution is an online operation and stays off
    unless the caller explicitly asks for it.
    """
    if not photo.get("exif"):
        path = photo.get("path") or photo.get("original_path")
        if path and os.path.exists(path):
            try:
                location, exif = extract_exif(path)
            except (OSError, ValueError):
                location, exif = None, {}
            if location or exif:
                database.update_exif(photo["id"], location, exif)
                photo["location"] = location
                photo["exif"] = exif

    exif = photo.get("exif") or {}
    if (
        resolve_location
        and cfg
        and geocoding_enabled(cfg.geocoding_provider)
        and photo.get("location")
        and exif.get("latitude") is not None
        and exif.get("longitude") is not None
        and "，" not in str(photo.get("location"))
        and not any(ch in str(photo.get("location")) for ch in "省市县区镇")
    ):
        place = reverse_geocode(
            exif["latitude"],
            exif["longitude"],
            cfg.geocoding_provider,
            cfg.geocoding_api_key,
            interval=cfg.geocoding_interval,
            retries=cfg.geocoding_retries,
            allow_fallback=cfg.geocoding_fallback,
        )
        if place:
            database.update_exif(photo["id"], place, exif)
            photo["location"] = place
    return photo


def _cleanup_cache(
    database: Database, cache_dir_name: str = ".photo-review-cache"
) -> dict[str, int]:
    """Delete unreferenced proxy files from every indexed folder's cache."""
    total_freed = 0
    total_size = 0
    total_photos = 0
    for folder in database.all_folders():
        result = cleanup_folder_cache(folder, database, cache_dir_name)
        total_freed += result["freed"]
        total_size += result["freed_size"]
        total_photos += result.get("photos_affected", 0)
    return {
        "freed": total_freed,
        "freed_size": total_size,
        "photos_affected": total_photos,
    }


def _job_payload(job) -> dict[str, Any]:
    """Shape one job for the API.

    ``started_at`` and ``elapsed`` ride along so a page that is refreshed in the
    middle of a job can recompute the remaining time from the whole run instead
    of starting its own measurement from zero.
    """
    return {
        "job_id": job.id,
        "folder": job.folder,
        "status": job.status,
        "total": job.total,
        "processed": job.processed,
        "current": job.current,
        "phase": job.phase,
        "report": job.report,
        "error": job.error,
        "cancelled": job.cancelled,
        "started_at": job.started_at,
        "elapsed": round(job.elapsed, 2),
    }


def _load_settings_from_db(database: Database, cfg: Config) -> None:
    """Load persisted settings from the SQLite ``settings`` table."""
    raw = database.get_settings()
    for key in PERSISTED_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        current = getattr(cfg, key, None)
        try:
            if isinstance(current, bool):
                setattr(
                    cfg, key, str(value).strip().lower() in {"1", "true", "yes", "on"}
                )
            elif isinstance(current, int):
                setattr(cfg, key, int(float(str(value).strip())))
            elif isinstance(current, float):
                setattr(cfg, key, float(str(value).strip()))
            else:
                setattr(cfg, key, value)
        except (TypeError, ValueError):
            continue

    # The stored 5-second preview predates the control actually working: until
    # this release ``POST /api/config`` dropped every video key, so the value in
    # the database can only be the old built-in default, never a choice. Move it
    # to the new default once; anything else is left alone.
    if _stored_video_clip_seconds(raw) == 5.0:
        cfg.video_clip_seconds = 3.0
        database.save_settings({"video_clip_seconds": 3.0})


def _stored_video_clip_seconds(raw: dict) -> float | None:
    try:
        return float(str(raw.get("video_clip_seconds", "")).strip())
    except (TypeError, ValueError):
        return None


def _save_settings_to_db(database: Database, cfg: Config) -> None:
    """Persist current config into the SQLite ``settings`` table."""
    data = {key: getattr(cfg, key) for key in PERSISTED_FIELDS}
    database.save_settings(data)


#: Settings rows written by older versions that the app no longer reads.
#: ``host``/``port``/``debug`` are environment-only now (a persisted
#: ``debug=true`` used to survive into a later start and open the Werkzeug
#: debugger); ``thumb_size`` was renamed to ``gallery_thumb_size``. Rows like
#: these are invisible in the UI but would silently win over the environment,
#: so they are dropped on start.
_LEGACY_SETTINGS_KEYS = frozenset(
    {
        "host",
        "port",
        "debug",
        "thumb_size",
        "settings_path",
        "image_extensions",
        "db_path",
    }
)


def _prune_legacy_settings(database: Database) -> int:
    """Delete settings rows that no longer map to a configuration field."""
    stored = database.get_settings()
    stale = sorted(
        key
        for key in stored
        if key not in PERSISTED_FIELDS and key in _LEGACY_SETTINGS_KEYS
    )
    if not stale:
        return 0
    database.delete_settings(stale)
    logger.info("removed %d stale settings rows: %s", len(stale), ", ".join(stale))
    return len(stale)


def _start_background_repairs(database: Database, cfg: Config) -> None:
    """Run pending one-off repairs and the scheduled backup without delaying startup.

    The orientation repair walks every file header in the library, which takes
    a second or two on a large album, so it runs in a daemon thread and the UI is
    usable immediately. The thread is started when *any* repair is still pending
    **or** the backup interval has elapsed; gating it on the orientation repair
    alone meant a database that had already been through that pass never got the
    pairing backfill, the dimension merge, or another backup.
    """
    if not repairs_pending(database) and not backup_due(database):
        return

    def worker() -> None:
        try:
            run_startup_repairs(database, cfg)
        except Exception:
            # A failed repair must never take the server down with it.
            logger.warning("startup repairs failed", exc_info=True)

    thread = threading.Thread(target=worker, name="lumina-repair", daemon=True)
    register_repair_thread(thread)
    thread.start()


