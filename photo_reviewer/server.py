"""Flask web application and REST API for the local photo review system."""

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

from flask import Flask, jsonify, request, send_file, url_for

from . import video
from .api_client import (
    DIMENSION_KEYS,
    LEGACY_DIMENSION_KEYS,
    PROMPT_PRESETS,
    SemanticSearchError,
    semantic_search,
    strip_json_format,
)
from .cache import (
    cache_dir_for_folder,
    cleanup_folder_cache,
    ensure_cache_dirs,
)
from .cards import (
    CardError,
    apply_pending,
    card_status,
    delete_from_card,
    forget_card,
    pending_preview,
    purge_for_photo,
    relative_to_album,
    restore_to_card,
    target_for,
)
from .config import (
    EDITABLE_FIELDS,
    PERSISTED_FIELDS,
    PREVIEW_MODES,
    VIDEO_PROCESSING,
    Config,
)
from .db import Database, sort_photos
from .exif import extract_exif
from .geocode import PROVIDERS as GEOCODING_PROVIDERS
from .geocode import geocoding_enabled, reverse_geocode
from .importer import ImportError_
from .paths import canonical_folder, canonical_path, is_in_named_dir, same_path
from .repair import (
    needs_orientation_repair,
    register_repair_thread,
    run_startup_repairs,
)
from .scanner import (
    JOBS,
    RAW_EXTENSIONS,
    reanalyze_photo,
    start_rebuild_index,
    start_scan,
    start_video_job,
    video_job_for,
)
from .sync import build_plan, start_import
from .thumbnailer import make_proxy

logger = logging.getLogger(__name__)

#: How many recently used folders the UI state keeps.
UI_STATE_HISTORY_LIMIT = 20

#: Methods that mutate state and therefore require a same-origin request.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: One year, in seconds: how long a content-addressed thumbnail is cacheable.
_IMMUTABLE_IMAGE_MAX_AGE = 31536000


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
                "recommendation": photo.get("recommendation"),
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
    for key in ("thumb_path", "proxy_path", "clip_path", "play_path", "live_path"):
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
    """Run one-off data repairs without delaying startup.

    The orientation repair walks every file header in the library, which takes
    a second or two on a large album. It runs in a daemon thread so the UI is
    usable immediately; the gallery picks the corrected sizes up on the next
    refresh. It is a no-op once the database records the repair as done.
    """
    if not needs_orientation_repair(database):
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


def create_app(config: Config | None = None, db: Database | None = None) -> Flask:
    from .config import CONFIG
    from .db import get_db

    cfg = config or CONFIG
    database = db or get_db(cfg.db_path)
    _prune_legacy_settings(database)
    _load_settings_from_db(database, cfg)
    _start_background_repairs(database, cfg)
    static_dir = str(Path(__file__).resolve().parent.parent / "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    app.config["JSON_AS_ASCII"] = False

    @app.before_request
    def _reject_cross_origin_writes():
        """Block CSRF on every mutating route.

        Flask parses a JSON body even when the browser sends it as
        ``text/plain``, which makes ordinary form posts and ``fetch`` calls from
        any other page able to drive ``/api/delete`` and friends. Requiring the
        browser-declared origin to match this host closes that hole without
        tokens or sessions; requests without an ``Origin``/``Referer`` header
        (curl, the test client) are still allowed.
        """
        if request.method not in _UNSAFE_METHODS:
            return None
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if not source:
            return None
        host = request.host
        if source in (f"http://{host}", f"https://{host}"):
            return None
        allowed = cfg.allowed_origins
        base = source.split("/", 3)
        if allowed and len(base) >= 3 and f"{base[0]}//{base[2]}" in allowed:
            return None
        logger.warning(
            "rejected cross-origin %s %s from %s", request.method, request.path, source
        )
        return jsonify({"error": "跨站请求已被拒绝"}), 403

    @app.get("/")
    def index():
        return send_file(os.path.join(static_dir, "index.html"))

    @app.get("/api/config")
    def api_config():
        return jsonify(cfg.to_dict())

    @app.get("/api/ui-state")
    def api_ui_state():
        """Return the browser-independent UI state.

        The folder history, the open album and the remembered card path used to
        live in ``localStorage``, so clearing the browser's site data threw the
        album's memory away. They are kept in the database instead, next to the
        photos they describe.
        """
        return jsonify({"state": database.get_ui_state()})

    @app.post("/api/ui-state")
    def api_save_ui_state():
        """Merge a partial UI state into the stored one.

        Only known, bounded keys are accepted: this endpoint is writable from the
        browser, so it must not become a way to stuff arbitrary rows into the
        database. Values are validated into a staging dict first, so a rejected
        field cannot leave half an update behind.
        """
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "请求内容不合法"}), 400
        staged: dict[str, Any] = {}

        if "folder" in data:
            staged["folder"] = str(data["folder"] or "").strip()[:512]

        if "folder_history" in data:
            raw = data["folder_history"]
            if not isinstance(raw, list):
                return jsonify({"error": "folder_history 必须是数组"}), 400
            history = []
            seen = set()
            for item in raw:
                text = str(item or "").strip()[:512]
                if not text:
                    continue
                key = os.path.normcase(text.rstrip("\\/"))
                if key in seen:
                    continue
                seen.add(key)
                history.append(text)
                if len(history) >= UI_STATE_HISTORY_LIMIT:
                    break
            staged["folder_history"] = history

        if "import_source" in data:
            staged["import_source"] = str(data["import_source"] or "").strip()[:512]

        if not staged:
            return jsonify({"error": "没有可保存的字段"}), 400
        database.save_ui_state(staged)
        return jsonify({"state": database.get_ui_state()})

    @app.post("/api/config")
    def api_update_config():
        """Apply settings from the UI.

        Every value is validated into a staging dict first; nothing touches
        ``cfg`` until the whole payload is known to be valid, so a rejected
        field can no longer leave the running process half-updated while the
        database keeps the old value.
        """
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "请求体必须是 JSON 对象"}), 400
        ignored = sorted(set(data) - EDITABLE_FIELDS)
        if ignored:
            logger.info("ignoring non-editable settings keys: %s", ", ".join(ignored))

        staged: dict[str, Any] = {}

        def as_int(key: str, minimum: int, maximum: int) -> str | None:
            if key not in data:
                return None
            try:
                staged[key] = max(minimum, min(maximum, int(data[key])))
            except (TypeError, ValueError):
                return f"{key} 参数不合法"
            return None

        def as_float(key: str, minimum: float, maximum: float) -> str | None:
            if key not in data:
                return None
            try:
                staged[key] = max(minimum, min(maximum, float(data[key])))
            except (TypeError, ValueError):
                return f"{key} 参数不合法"
            return None

        def as_bool(key: str) -> str | None:
            if key not in data:
                return None
            staged[key] = bool(data[key])
            return None

        checks = (
            as_int("proxy_max_edge", 64, 8192),
            as_int("gallery_thumb_size", 96, 4096),
            as_int("proxy_quality", 1, 100),
            as_int("thumb_quality", 1, 100),
            as_int("request_timeout", 5, 3600),
            as_int("model_retries", 0, 10),
            as_int("scan_concurrency", 1, 16),
            as_int("index_concurrency", 1, 32),
            as_int("geocoding_retries", 0, 10),
            as_float("geocoding_interval", 0.1, 10.0),
            # The video block. These keys were listed as editable but never
            # staged, so the dialog accepted a change and silently dropped it -
            # which is why the preview length kept snapping back to its old
            # value. Every numeric setting is clamped here, not in the browser.
            as_float("video_clip_seconds", 2.0, 20.0),
            as_int("video_clip_max_edge", 320, 1920),
            as_int("video_clip_crf", 18, 40),
            as_float("video_poster_ratio", 0.0, 0.5),
            as_int("video_preview_volume", 0, 100),
            as_int("live_volume", 0, 100),
            as_bool("video_clip_audio"),
            as_bool("video_pregenerate"),
            as_bool("merge_live_photos"),
        )
        for error in checks:
            if error:
                return jsonify({"error": error}), 400

        if "video_preview_mode" in data:
            mode = str(data["video_preview_mode"]).strip().lower()
            if mode not in PREVIEW_MODES:
                return jsonify({"error": "video_preview_mode 参数不合法"}), 400
            staged["video_preview_mode"] = mode

        if "video_processing" in data:
            processing = str(data["video_processing"]).strip().lower()
            if processing not in VIDEO_PROCESSING:
                return jsonify({"error": "video_processing 参数不合法"}), 400
            staged["video_processing"] = processing

        if "api_base_url" in data:
            staged["api_base_url"] = (
                str(data["api_base_url"]).strip() or cfg.api_base_url
            )
        if "api_key" in data:
            staged["api_key"] = str(data["api_key"])
        if "model" in data:
            staged["model"] = str(data["model"]).strip() or cfg.model
        if "system_prompt" in data:
            staged["system_prompt"] = strip_json_format(str(data["system_prompt"]))
        if "geocoding_provider" in data:
            raw_provider = str(data["geocoding_provider"]).strip().lower()
            if raw_provider not in GEOCODING_PROVIDERS:
                # Anything unrecognised falls back to the free provider, which
                # is what ``reverse_geocode`` itself does - an empty value used
                # to mean "nominatim" and must keep meaning that.
                logger.debug("unknown geocoding provider %r, using nominatim", raw_provider)
                raw_provider = "nominatim"
            staged["geocoding_provider"] = raw_provider
        if "geocoding_api_key" in data:
            staged["geocoding_api_key"] = str(data["geocoding_api_key"])

        for key, value in staged.items():
            setattr(cfg, key, value)
        _save_settings_to_db(database, cfg)
        return jsonify(cfg.to_dict())

    @app.get("/api/prompt-presets")
    def api_prompt_presets():
        return jsonify({"presets": PROMPT_PRESETS, "default": "playful"})

    @app.get("/api/dirs")
    def api_dirs():
        """List the sub-directories of one path, for the folder browser.

        The app's own directories are filtered out: ``.photo-review-cache`` and
        ``.photo-trash`` are hidden bookkeeping, and offering them as photo
        folders invites a user to index or delete their own cache by hand.
        """
        path = request.args.get("path", "").strip()
        if not path:
            path = str(Path.home())
        path = canonical_path(path)
        if not os.path.isdir(path):
            return jsonify({"error": "目录不存在"}), 404
        try:
            entries = sorted(os.listdir(path), key=str.lower)
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
        hidden = {cfg.cache_dir_name, cfg.trash_dir_name}
        dirs = []
        for name in entries:
            if name in hidden:
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full) and not os.path.islink(full):
                dirs.append({"name": name, "path": full})
        parent = os.path.dirname(path)
        return jsonify({"path": path, "parent": parent, "directories": dirs})

    @app.post("/api/open-folder")
    def api_open_folder():
        data = request.get_json(force=True, silent=True) or {}
        path = data.get("path", "").strip()
        if not path:
            return jsonify({"error": "请提供文件夹路径"}), 400
        path = canonical_path(path)
        if not os.path.isdir(path):
            return jsonify({"error": "文件夹不存在"}), 404
        try:
            _open_with_system(path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True})

    @app.post("/api/open-file")
    def api_open_file():
        """Hand one indexed photo (or its raw sibling) to the OS default app.

        The path is never trusted on its own: it must match either the stored
        ``path`` or the stored ``raw_path`` of an existing photo row, so this
        cannot be used to launch arbitrary files through the browser.
        """
        data = request.get_json(force=True, silent=True) or {}
        try:
            photo_id = int(data.get("photo_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "缺少照片 id"}), 400
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404

        candidates = [photo.get("path"), photo.get("raw_path"), photo.get("original_path")]
        requested = str(data.get("path") or "").strip()
        allowed = [c for c in candidates if c]
        if requested:
            resolved = canonical_path(requested)
            if not any(same_path(resolved, c) for c in allowed):
                return jsonify({"error": "该路径不属于这张照片"}), 403
        else:
            resolved = canonical_path(allowed[0]) if allowed else ""
        if not resolved or not os.path.isfile(resolved):
            return jsonify({"error": "文件不存在"}), 404
        try:
            _open_with_system(resolved)
        except Exception as exc:
            logger.warning("could not open %s", resolved, exc_info=True)
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True, "path": resolved})

    @app.post("/api/rebuild-index")
    def api_rebuild_index():
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip()
        force = bool(data.get("force", False))
        skip_videos = bool(data.get("skip_indexed_videos", False))
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        try:
            folder = _normalize_folder(folder)
        except NotADirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        for existing in JOBS.list():
            if existing.folder == folder and existing.status == "running":
                return jsonify(
                    {"error": "该文件夹已有任务正在运行", "job_id": existing.id}
                ), 409
        job = start_rebuild_index(
            folder, database, cfg, force=force, skip_indexed_videos=skip_videos
        )
        return jsonify(
            {
                "job_id": job.id,
                "status": job.status,
                "folder": folder,
                "phase": job.phase,
            }
        )

    @app.post("/api/scan")
    def api_scan():
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip()
        force = bool(data.get("force", False))
        # The answer to "ffmpeg is missing - keep the videos I already read".
        skip_videos = bool(data.get("skip_indexed_videos", False))
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        try:
            folder = _normalize_folder(folder)
        except NotADirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        for existing in JOBS.list():
            if existing.folder == folder and existing.status == "running":
                return jsonify(
                    {"error": "该文件夹已有扫描任务正在运行", "job_id": existing.id}
                ), 409
        job = start_scan(
            folder, database, cfg, force=force, skip_indexed_videos=skip_videos
        )
        return jsonify({"job_id": job.id, "status": job.status, "folder": folder})

    @app.post("/api/import/plan")
    def api_import_plan():
        """Preview an SD-card import without copying anything."""
        data = request.get_json(force=True, silent=True) or {}
        try:
            plan = build_plan(data, cfg)
        except ImportError_ as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"plan": plan.summary()})

    @app.post("/api/import/run")
    def api_import_run():
        """Copy the new files from a card, then index what arrived."""
        data = request.get_json(force=True, silent=True) or {}
        try:
            job = start_import(data, database, cfg)
        except ImportError_ as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "job_id": job.id,
                "status": job.status,
                "phase": job.phase,
                "folder": job.folder,
                "total": job.total,
            }
        )

    @app.get("/api/card/status")
    def api_card_status():
        """Which camera albums exist, whether their card is here, and the queue.

        Cheap on purpose (``isdir``, ``os.access``, one ``os.stat`` per card), so
        the toolbar can poll it to keep the "SD card connected" badge honest.
        """
        folder = request.args.get("folder", "").strip() or None
        folder = canonical_path(folder) if folder else None
        cards = card_status(database, folder)
        current = None
        if folder:
            current = next((c for c in cards if c["album"] == folder), None)
        return jsonify({"cards": cards, "current": current})

    @app.post("/api/card/pending")
    def api_card_pending():
        """Preview the queued card work without touching the card."""
        data = request.get_json(force=True, silent=True) or {}
        source = data.get("source", "").strip() or None
        return jsonify({"preview": pending_preview(database, source)})

    @app.post("/api/card/sync")
    def api_card_sync():
        """Carry the queued deletions and restores out on the card."""
        data = request.get_json(force=True, silent=True) or {}
        source = canonical_path(data.get("source", "").strip())
        if not source:
            return jsonify({"error": "请提供 SD 卡路径"}), 400
        include_raw = _as_bool(data.get("include_raw", True), default=True)
        result = apply_pending(database, source, cfg.trash_dir_name, include_raw)
        return jsonify(result)

    @app.post("/api/card/forget")
    def api_card_forget():
        """Stop syncing a folder with its card. Never deletes any file."""
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip()
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        source = data.get("source", "").strip() or None
        result = forget_card(database, canonical_path(folder), source)
        return jsonify(result)

    @app.get("/api/scan/jobs")
    def api_scan_jobs():
        folder = request.args.get("folder", "").strip() or None
        jobs = []
        for job in JOBS.list():
            if folder and job.folder != folder:
                continue
            jobs.append(_job_payload(job))
        jobs.sort(key=lambda item: (item["status"] != "running", item["job_id"]))
        return jsonify({"jobs": jobs})

    @app.get("/api/scan/status/<job_id>")
    def api_scan_status(job_id: str):
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify(_job_payload(job))

    @app.post("/api/scan/cancel/<job_id>")
    def api_scan_cancel(job_id: str):
        ok = JOBS.cancel(job_id)
        return jsonify({"cancelled": ok})

    @app.get("/api/tags")
    def api_tags():
        folder = request.args.get("folder", "").strip() or None
        return jsonify({"tags": database.all_tags(folder)})

    @app.get("/api/years")
    def api_years():
        folder = request.args.get("folder", "").strip() or None
        return jsonify({"years": database.capture_years(folder)})

    @app.get("/api/folders")
    def api_folders():
        return jsonify({"folders": database.all_folders()})

    @app.get("/api/photos")
    def api_photos():
        folder = request.args.get("folder", "").strip() or None
        status_raw = request.args.get("status", "").strip()
        recommendation = request.args.get("recommendation", "").strip() or None
        search = request.args.get("search", "").strip() or None
        tag = request.args.get("tag", "").strip() or None
        favorite = request.args.get("favorite", "").strip()
        year_raw = request.args.get("year", "").strip()
        year = None
        if year_raw:
            try:
                year = int(year_raw)
            except (TypeError, ValueError):
                return jsonify({"error": "year 参数不合法"}), 400
        status = None
        exclude_deleted = True
        if status_raw == "analyzed":
            status = "analyzed"
        elif status_raw == "pending":
            status = "pending"
        elif status_raw == "deleted":
            status = "deleted"
            exclude_deleted = False
        elif status_raw == "favorite":
            favorite = "1"
        # empty or "all" -> active only
        if favorite in ("1", "true", "yes", "on"):
            favorite = True
        elif favorite in ("0", "false", "no", "off"):
            favorite = False
        else:
            favorite = None
        sort = request.args.get("sort", "score_asc")
        kind_raw = request.args.get("kind", "").strip()
        kind = kind_raw if kind_raw in ("photo", "video", "live") else None
        try:
            min_score = (
                float(request.args["min_score"])
                if "min_score" in request.args
                else None
            )
            max_score = (
                float(request.args["max_score"])
                if "max_score" in request.args
                else None
            )
        except (TypeError, ValueError):
            return jsonify({"error": "score 参数不合法"}), 400
        try:
            requested_limit = min(int(request.args.get("limit", 200)), 500)
            offset = max(int(request.args.get("offset", 0)), 0)
        except (TypeError, ValueError):
            return jsonify({"error": "limit/offset 参数不合法"}), 400
        requested_limit = max(1, requested_limit)
        fetch_limit = requested_limit + 1
        photos = database.list_photos(
            folder=folder,
            status=status,
            recommendation=recommendation,
            min_score=min_score,
            max_score=max_score,
            search=search,
            tag=tag,
            favorite=favorite,
            year=year,
            exclude_deleted=exclude_deleted,
            sort=sort,
            limit=fetch_limit,
            offset=offset,
            kind=kind,
        )
        has_more = len(photos) > requested_limit
        photos = photos[:requested_limit]
        # No filesystem access here: EXIF was already backfilled by the index
        # phase, and a sleeping external drive must not stall every page.
        return jsonify(
            {
                "photos": [_photo_payload(p) for p in photos],
                "has_more": has_more,
            }
        )

    @app.post("/api/search")
    def api_search():
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip() or None
        query = data.get("query", "").strip()
        mode = data.get("mode", "smart") or "smart"
        status_raw = data.get("status", "").strip()
        recommendation = data.get("recommendation", "").strip() or None
        tag = data.get("tag", "").strip() or None
        favorite = data.get("favorite", "")
        year_raw = data.get("year")
        year = None
        if year_raw not in (None, ""):
            try:
                year = int(year_raw)
            except (TypeError, ValueError):
                return jsonify({"error": "year 参数不合法"}), 400
        status = None
        exclude_deleted = True
        if status_raw == "analyzed":
            status = "analyzed"
        elif status_raw == "pending":
            status = "pending"
        elif status_raw == "deleted":
            status = "deleted"
            exclude_deleted = False
        elif status_raw == "favorite":
            favorite = "1"
        if favorite in (1, "1", "true", "yes", "on"):
            favorite = True
        elif favorite in (0, "0", "false", "no", "off"):
            favorite = False
        else:
            favorite = None
        sort = data.get("sort", "score_asc")
        try:
            min_score = (
                float(data["min_score"])
                if data.get("min_score") not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            return jsonify({"error": "score 参数不合法"}), 400
        all_candidates = database.list_photos(
            folder=folder,
            status=status,
            recommendation=recommendation,
            min_score=min_score,
            tag=tag,
            favorite=favorite,
            year=year,
            exclude_deleted=exclude_deleted,
            sort=sort,
            limit=2000,
        )
        result_map = {}

        if mode in ("keyword", "smart"):
            keyword_photos = database.list_photos(
                folder=folder,
                status=status,
                recommendation=recommendation,
                min_score=min_score,
                search=query,
                tag=tag,
                favorite=favorite,
                year=year,
                exclude_deleted=exclude_deleted,
                sort=sort,
                limit=2000,
            )
            for p in keyword_photos:
                result_map[p["id"]] = p

        semantic_warning = None
        if mode in ("semantic", "smart") and query:
            try:
                ids = semantic_search(query, all_candidates, cfg)
                by_id = {p["id"]: p for p in all_candidates}
                for pid in ids:
                    if pid in by_id:
                        result_map[pid] = by_id[pid]
            except SemanticSearchError as exc:
                semantic_warning = str(exc)
                if mode == "semantic" and not result_map:
                    return jsonify({"error": semantic_warning}), 502

        photos = sort_photos(list(result_map.values()), sort)
        return jsonify(
            {
                "photos": [_photo_payload(p) for p in photos],
                "warning": semantic_warning,
            }
        )

    @app.get("/api/stats")
    def api_stats():
        folder = request.args.get("folder", "").strip() or None
        return jsonify(database.stats(folder))

    @app.get("/api/photo/<int:photo_id>")
    def api_photo(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        photo = _ensure_exif(photo, database, cfg)
        return jsonify(_photo_payload(photo, detail=True))

    @app.get("/api/photo/<int:photo_id>/detail")
    def api_photo_detail(photo_id: int):
        """The detail-only extras for the photo the user just opened.

        The detail dialog needs three things the gallery list deliberately
        omits: the EXIF block (for the camera/spec row and the capture time),
        the raw sibling's size (for "10 MB + 30 MB") and the true pixel size.
        Sending them with every list row cost ~30% of the payload plus one
        ``stat()`` per photo; asking for them once per opened photo costs one
        small request, and the dialog paints its frame before this lands.
        """
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        photo = _ensure_exif(photo, database, cfg)
        raw_path = photo.get("raw_path")
        is_video = (photo.get("media_kind") or "photo") == "video"
        return jsonify(
            {
                "id": photo["id"],
                "exif": photo.get("exif") or {},
                "raw_size": _file_size(raw_path),
                "width": photo.get("width"),
                "height": photo.get("height"),
                "size": photo.get("size"),
                "location": photo.get("location"),
                "media_kind": photo.get("media_kind") or "photo",
                "duration_ms": photo.get("duration_ms"),
                "video_info": photo.get("video_info") or {} if is_video else {},
                "has_live": bool(photo.get("live_path")),
                "clip_url": (
                    url_for("api_video_clip", photo_id=photo["id"]) if is_video else None
                ),
                "play_state": photo.get("play_state") or "",
                "ffmpeg": video.tools_available(cfg.ffmpeg_path or None),
            }
        )

    @app.get("/api/thumbnail/<int:photo_id>")
    def api_thumbnail(photo_id: int):
        """Serve the small gallery thumbnail.

        Lookup order is ``thumb_path`` → sibling of ``proxy_path`` → generate.
        Only the last branch decodes the picture, so a cached thumbnail is
        served straight from disk and a raw file never needs the sensor data.

        ``?v=<hash>`` marks the URL as content-addressed: the client sends the
        row's ``thumb_version``, which changes whenever the file is re-rendered.
        Those responses are cached for a year, so scrolling a long album back
        and forth costs no requests at all. Without the parameter (an old
        bookmark, a manual visit) the response stays revalidated, because the
        file behind a bare id can change under it.
        """
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        cache_timeout = _image_cache_timeout()
        thumb = _thumbnail_path(photo)
        if thumb:
            return send_file(
                thumb,
                mimetype="image/jpeg",
                conditional=True,
                max_age=cache_timeout,
            )
        path = photo.get("path") or photo.get("original_path")
        if path and os.path.exists(path):
            try:
                folder = photo.get("folder") or os.path.dirname(path)
                thumb_dir = ensure_cache_dirs(folder, cfg.cache_dir_name)
                proxy_path, _, _, phash, thumb_path = make_proxy(
                    path,
                    max_edge=cfg.proxy_max_edge,
                    cache_dir=str(thumb_dir),
                    quality=cfg.proxy_quality,
                    thumb_size=cfg.gallery_thumb_size,
                    thumb_quality=cfg.thumb_quality,
                )
                database.update_cache_paths(photo_id, thumb_path, proxy_path, phash)
                return send_file(
                    thumb_path,
                    mimetype="image/jpeg",
                    conditional=True,
                    max_age=cache_timeout,
                )
            except (OSError, ValueError):
                logger.warning(
                    "thumbnail generation failed for photo %s", photo_id, exc_info=True
                )
                return jsonify({"error": "无法生成缩略图"}), 500
        return jsonify({"error": "缩略图不存在"}), 404

    @app.get("/api/proxy/<int:photo_id>")
    def api_proxy(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        proxy = photo.get("proxy_path")
        if not proxy or not os.path.exists(proxy):
            path = photo.get("path") or photo.get("original_path")
            if not path or not os.path.exists(path):
                return jsonify({"error": "代理图不存在"}), 404
            try:
                folder = photo.get("folder") or os.path.dirname(path)
                proxy_dir = ensure_cache_dirs(folder, cfg.cache_dir_name)
                proxy, _, _, _, _ = make_proxy(
                    path,
                    max_edge=cfg.proxy_max_edge,
                    cache_dir=str(proxy_dir),
                    quality=cfg.proxy_quality,
                    thumb_size=cfg.gallery_thumb_size,
                    thumb_quality=cfg.thumb_quality,
                )
                database.update_proxy_path(photo_id, proxy)
            except (OSError, ValueError):
                logger.warning(
                    "proxy generation failed for photo %s", photo_id, exc_info=True
                )
                return jsonify({"error": "无法生成代理图"}), 500
        return send_file(proxy, mimetype="image/jpeg", conditional=True)

    @app.get("/api/original/<int:photo_id>")
    def api_original(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        path = photo.get("path") or photo.get("original_path")
        if not path or not os.path.exists(path):
            return jsonify({"error": "原图不存在"}), 404
        return send_file(path, conditional=True)

    # ------------------------------------------------------------------
    # Video
    #
    # Three URLs, one for each thing the UI shows: the five-second preview the
    # detail dialog loops, the original file played full screen, and a
    # browser-safe copy for the containers a browser refuses. Range requests are
    # handled by ``send_file`` itself, which is what makes seeking work without
    # any hand-written byte-range code.
    # ------------------------------------------------------------------

    @app.get("/api/video/status")
    def api_video_status():
        """Whether the video pipeline can run on this machine.

        The settings dialog shows this: ffmpeg is a system dependency, so the
        user has to be told "not found" somewhere visible rather than discovering
        it when a card has no picture.
        """
        available = video.tools_available(cfg.ffmpeg_path or None)
        return jsonify(
            {
                "available": available,
                "version": video.tools_version(cfg.ffmpeg_path or None) if available else "",
                "extensions": list(cfg.video_extensions),
            }
        )

    def _video_row(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo or (photo.get("media_kind") or "photo") != "video":
            return None
        return photo

    def _video_cache_dir(photo: dict[str, Any]) -> str:
        folder = photo.get("folder") or os.path.dirname(photo["path"])
        return str(ensure_cache_dirs(folder, cfg.cache_dir_name))

    def _cache_key(photo: dict[str, Any]) -> str:
        return video.video_cache_key(photo["path"])

    def _stream_response(path: str, mimetype: str):
        """Send a media file with the headers a ``<video>`` element expects."""
        response = send_file(
            path,
            mimetype=mimetype,
            conditional=True,
            max_age=_IMMUTABLE_IMAGE_MAX_AGE,
        )
        # Werkzeug answers Range requests but does not advertise it on every
        # response; players use the header to decide whether seeking is possible.
        response.headers["Accept-Ranges"] = "bytes"
        return response

    def _guess_mimetype(path: str) -> str:
        ext = Path(path).suffix.lower()
        return {
            ".mp4": "video/mp4",
            ".m4v": "video/mp4",
            ".mov": "video/quicktime",
            ".qt": "video/quicktime",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
            ".avi": "video/x-msvideo",
            ".ts": "video/mp2t",
            ".mts": "video/mp2t",
            ".m2ts": "video/mp2t",
            ".3gp": "video/3gpp",
            ".ogv": "video/ogg",
            ".wmv": "video/x-ms-wmv",
        }.get(ext, "video/mp4")

    @app.get("/api/video/<int:photo_id>/clip")
    def api_video_clip(photo_id: int):
        """The short preview the detail dialog loops.

        Generated on first open (~200 ms) and cached next to the poster. The
        dialog paints the poster immediately and cross-fades to this once it can
        play, so the delay is invisible.
        """
        photo = _video_row(photo_id)
        if not photo:
            return jsonify({"error": "视频不存在"}), 404
        clip_path = photo.get("clip_path")
        if not clip_path or not os.path.exists(clip_path):
            if not video.tools_available(cfg.ffmpeg_path or None):
                return jsonify({"error": "未安装 ffmpeg，无法生成预览"}), 503
            cache_dir = _video_cache_dir(photo)
            clip_path = os.path.join(cache_dir, f"{_cache_key(photo)}_clip.mp4")
            info = video.probe(photo["path"], cfg.ffmpeg_path or None)
            if not video.make_clip(
                photo["path"],
                clip_path,
                info,
                seconds=cfg.video_clip_seconds,
                max_edge=cfg.video_clip_max_edge,
                crf=cfg.video_clip_crf,
                include_audio=cfg.video_clip_audio,
                override_dir=cfg.ffmpeg_path or None,
            ):
                return jsonify({"error": "无法生成视频预览"}), 500
            database.set_video_paths(photo_id, clip_path=clip_path)
        return _stream_response(clip_path, "video/mp4")

    @app.get("/api/video/<int:photo_id>/stream")
    def api_video_stream(photo_id: int):
        """The original file, byte ranges included.

        This is what the full-screen player uses: no transcoding, full quality,
        and the browser's own seek handling.
        """
        photo = _video_row(photo_id)
        if not photo:
            return jsonify({"error": "视频不存在"}), 404
        path = photo.get("path")
        if not path or not os.path.exists(path):
            return jsonify({"error": "视频文件不存在"}), 404
        return _stream_response(path, _guess_mimetype(path))

    @app.get("/api/video/<int:photo_id>/playable")
    def api_video_playable(photo_id: int):
        """A copy every desktop browser can decode.

        Tried only after the original failed to play, which is the client's cue
        that the codec or the container is the problem. A stream copy is enough
        for the common case (MKV/MTS carrying H.264); a genuine transcode runs
        as a tracked job so the toolbar can show its progress.

        Returns 200 with the file when it is ready, 202 with a job id while it is
        being prepared, and 503 when ffmpeg is missing.
        """
        photo = _video_row(photo_id)
        if not photo:
            return jsonify({"error": "视频不存在"}), 404
        play_path = photo.get("play_path")
        if play_path and os.path.exists(play_path):
            return _stream_response(play_path, "video/mp4")
        if not video.tools_available(cfg.ffmpeg_path or None):
            return jsonify({"error": "未安装 ffmpeg，无法转换此视频"}), 503
        running = video_job_for(photo_id)
        if running:
            return jsonify({"job_id": running.id, "state": "running"}), 202
        job = start_video_job(photo_id, database, cfg)
        return jsonify({"job_id": job.id, "state": job.status}), 202

    @app.get("/api/photo/<int:photo_id>/live")
    def api_live_video(photo_id: int):
        """The moving part of a motion photo or Live Photo."""
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        live_path = photo.get("live_path")
        if not live_path or not os.path.exists(live_path):
            return jsonify({"error": "这张照片没有实况视频"}), 404
        return _stream_response(live_path, _guess_mimetype(live_path))

    @app.post("/api/delete")
    def api_delete():
        """Move photos to the trash, and optionally off the camera card too.

        ``sync_card`` is the answer to the "also delete from the SD card"
        checkbox, which the UI only offers while that card is connected. When the
        card is away the answer is false by construction and the deletion is
        queued for the next time the card shows up.
        """
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请选择要删除的照片"}), 400
        sync_card = _as_bool(data.get("sync_card", False))
        moved = []
        errors = []
        touched_folders = set()
        card_totals = {"synced": 0, "queued": 0, "skipped": 0, "errors": []}
        any_card = False
        for pid in ids:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                errors.append({"id": pid, "error": "无效 ID"})
                continue
            photo = database.get_photo(pid)
            if not photo:
                errors.append({"id": pid, "error": "照片不存在"})
                continue
            if photo.get("status") == "deleted":
                errors.append({"id": pid, "error": "照片已在回收站"})
                continue
            # The card side is decided before the local move, because moving the
            # raw sibling clears raw_path and loses the half of the pair that
            # also has to disappear from the card.
            raw_path = photo.get("raw_path")
            try:
                card_result = _sync_delete_to_card(
                    database, cfg, photo, raw_path, sync_card
                )
            except Exception:
                logger.warning("card sync failed for photo %s", pid, exc_info=True)
                card_result = None
            try:
                new_path, trashed_raw = _move_to_trash(photo, cfg)
                database.mark_deleted([photo["id"]], {photo["id"]: new_path})
                if photo.get("raw_path"):
                    # The raw is no longer beside the image, but the row keeps
                    # pointing at where it went: the pair is still a pair, and
                    # "彻底移除" has to be able to offer deleting both halves.
                    database.update_raw_path(photo["id"], trashed_raw)
                moved.append({"id": photo["id"], "path": new_path})
                touched_folders.add(
                    photo.get("folder") or os.path.dirname(photo["path"])
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": pid, "error": str(exc)})
                continue
            if card_result:
                any_card = True
                for key in ("synced", "queued", "skipped"):
                    card_totals[key] += card_result[key]
                card_totals["errors"].extend(card_result["errors"])
                card_totals["source"] = card_result["source"]
        # No folder-wide cache sweep here: moving a photo to the trash keeps its
        # row and every cached path it references, so a sweep would scan the
        # whole cache directory to find nothing. It only earns its cost when a
        # row is actually deleted, which is what "彻底移除" does.
        payload = {"moved": moved, "errors": errors}
        if any_card:
            payload["card"] = card_totals
        return jsonify(payload)

    @app.post("/api/restore")
    def api_restore():
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请选择要恢复的照片"}), 400
        sync_card = _as_bool(data.get("sync_card", False))
        restored = []
        errors = []
        touched_folders = set()
        card_totals = {"restored": 0, "queued": 0, "errors": []}
        any_card = False
        for pid in ids:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                errors.append({"id": pid, "error": "无效 ID"})
                continue
            photo = database.get_photo(pid)
            if not photo:
                errors.append({"id": pid, "error": "照片不存在"})
                continue
            if photo.get("status") != "deleted":
                errors.append({"id": pid, "error": "照片不在回收站"})
                continue
            try:
                new_original, restored_raw = _restore_from_trash(photo, config=cfg)
                database.restore_deleted([photo["id"]], {photo["id"]: new_original})
                if restored_raw:
                    database.update_raw_path(photo["id"], restored_raw)
                restored.append({"id": photo["id"], "path": new_original})
                touched_folders.add(
                    photo.get("folder")
                    or os.path.dirname(photo.get("original_path") or photo["path"])
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": pid, "error": str(exc)})
                continue
            try:
                card_result = _sync_restore_to_card(
                    database, photo, restored_raw, sync_card
                )
            except Exception:
                logger.warning("card restore failed for photo %s", pid, exc_info=True)
                card_result = None
            if card_result:
                any_card = True
                card_totals["restored"] += card_result["restored"]
                card_totals["queued"] += card_result["queued"]
                card_totals["errors"].extend(card_result["errors"])
                card_totals["source"] = card_result["source"]
        # Same reasoning as delete: restoring reuses the cache the row still
        # points at, so there is nothing to sweep.
        payload = {"restored": restored, "errors": errors}
        if any_card:
            payload["card"] = card_totals
        return jsonify(payload)

    @app.post("/api/delete/permanent")
    def api_delete_permanent():
        """Permanently remove photos that are already in the trash.

        ``delete_raw`` (default on) also removes the raw sibling that was moved
        to the trash with the photo.
        """
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请选择要永久删除的照片"}), 400
        delete_raw = _as_bool(data.get("delete_raw", True), default=True)
        purged = []
        errors = []
        raw_removed = 0
        live_removed = 0
        card_removed = 0
        card_deferred = 0
        touched_folders = set()
        for pid in ids:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                errors.append({"id": pid, "error": "无效 ID"})
                continue
            photo = database.get_photo(pid)
            if not photo:
                errors.append({"id": pid, "error": "照片不存在"})
                continue
            if photo.get("status") != "deleted":
                errors.append({"id": pid, "error": "只有已删除的照片可以永久删除"})
                continue
            path = photo.get("path") or photo.get("original_path")
            file_removed = False
            if path and os.path.exists(path):
                if not _trash_only(path, cfg):
                    errors.append(
                        {
                            "id": pid,
                            "error": "该记录指向的不是回收站文件，已跳过以免误删原图",
                        }
                    )
                    continue
                try:
                    _remove_file(path)
                    file_removed = True
                except OSError as exc:
                    errors.append({"id": pid, "error": f"删除文件失败: {exc}"})
                    continue
            # The raw sibling sits in the same trash day folder; purging the
            # photo must not leave it behind.
            if delete_raw:
                sibling = _trashed_raw_candidate(photo)
                if sibling and _trash_only(sibling, cfg):
                    try:
                        _remove_file(sibling)
                        raw_removed += 1
                    except OSError:
                        logger.debug("raw sibling already gone: %s", sibling)
            # A Live Photo's movie went to the trash with the still, so purging
            # the photo has to take it too - otherwise "彻底移除" leaves a movie
            # in the trash with no card pointing at it.
            live_sibling = _trashed_live_candidate(photo, cfg)
            if live_sibling and _trash_only(live_sibling, cfg):
                try:
                    _remove_file(live_sibling)
                    live_removed += 1
                except OSError:
                    logger.debug("live photo movie already gone: %s", live_sibling)
            # A file that was moved into the card's trash earlier is erased now,
            # so permanently removing a photo does not leave a copy behind on the
            # card. A card that is away defers this to its next connection.
            try:
                card_result = purge_for_photo(
                    database, target_for(database, photo.get("folder") or ""), pid
                )
                card_removed += card_result["purged"]
                card_deferred += card_result["deferred"]
            except (CardError, OSError):
                logger.warning("card purge failed for photo %s", pid, exc_info=True)
            touched_folders.add(
                photo.get("folder")
                or os.path.dirname(photo.get("original_path") or photo["path"])
            )
            _remove_photo_cache(photo, cfg)
            database.delete_rows([pid])
            purged.append({"id": pid, "file_removed": file_removed})
        for folder in touched_folders:
            try:
                cleanup_folder_cache(folder, database, cfg.cache_dir_name)
            except (OSError, sqlite3.Error):
                logger.warning("cache cleanup failed for %s", folder, exc_info=True)
        return jsonify(
            {
                "purged": purged,
                "raw_removed": raw_removed,
                "live_removed": live_removed,
                "card_removed": card_removed,
                "card_deferred": card_deferred,
                "errors": errors,
            }
        )

    @app.post("/api/folder/remove")
    def api_folder_remove():
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip()
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        folder = canonical_path(folder)
        for existing in JOBS.list():
            if existing.folder == folder and existing.status == "running":
                return jsonify({"error": "该文件夹正在整理中，请稍后再试"}), 409
        removed = database.remove_folder(folder)
        # The folder is no longer indexed, so it is no longer a camera album
        # either; forgetting the pairing drops queued card work with it. No file
        # on the card is touched by this.
        links = forget_card(database, folder)
        cache_dir = cache_dir_for_folder(folder, cfg.cache_dir_name)
        cache_removed = False
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_removed = not cache_dir.exists()
        return jsonify(
            {
                "removed": removed,
                "cache_removed": cache_removed,
                "card_links_removed": links["pairings_removed"],
            }
        )

    @app.post("/api/photo/<int:photo_id>/edit")
    def api_photo_edit(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        data = request.get_json(force=True, silent=True) or {}
        try:
            score = float(data.get("score", photo.get("score") or 0.0))
            score = max(0.0, min(10.0, score))
        except (TypeError, ValueError):
            return jsonify({"error": "总分不合法"}), 400
        dimensions = _clean_dimensions(photo.get("dimensions"))
        raw_dimensions = data.get("dimensions") or {}
        if isinstance(raw_dimensions, dict):
            for key in DIMENSION_KEYS:
                if key in raw_dimensions:
                    try:
                        dimensions[key] = max(
                            0.0, min(10.0, float(raw_dimensions[key]))
                        )
                    except (TypeError, ValueError):
                        return jsonify({"error": f"{key} 评分不合法"}), 400
        tags = data.get("tags", photo.get("tags") or [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
        if not isinstance(tags, list):
            return jsonify({"error": "标签不合法"}), 400
        tags = [str(t).strip() for t in tags if str(t).strip()][:20]
        reason = str(data.get("reason", photo.get("reason") or "")).strip()
        title = str(data.get("title", photo.get("title") or "")).strip()[:40]
        location_value = data.get("location", photo.get("location") or "")
        location = str(location_value).strip() if location_value else None
        database.update_metadata(
            photo_id, score, dimensions, tags, reason, title, location
        )
        updated = database.get_photo(photo_id)
        return jsonify(_photo_payload(updated))

    @app.post("/api/trash/empty")
    def api_trash_empty():
        """Permanently delete everything in the trash.

        ``delete_raw`` (default on) also removes the raw sibling that was moved
        to the trash together with each photo, so emptying the bin does not
        leave orphaned raw files behind.

        ``background`` (used by the UI) runs the sweep as a tracked job and
        returns its id, so the toolbar can show a progress bar. The synchronous
        form is kept for scripts and tests, which want the counts directly.
        """
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip() or None
        delete_raw = _as_bool(data.get("delete_raw", True), default=True)
        background = _as_bool(data.get("background", False), default=False)
        if folder:
            folder = canonical_path(folder)

        if background:
            # Count before starting the thread: the progress bar needs a total
            # from the very first tick, and a caller that polls immediately
            # should not see "0/0" and conclude there is nothing to do.
            pending = database.list_photos(
                folder=folder,
                status="deleted",
                exclude_deleted=False,
                limit=100000,
            )
            job = JOBS.create(folder or "", False)
            job.phase = "trash"
            job.total = len(pending)
            thread = threading.Thread(
                target=_empty_trash_worker,
                args=(job.id, database, cfg, folder, delete_raw),
                daemon=True,
            )
            thread.start()
            return jsonify({"job_id": job.id, "status": job.status, "total": job.total})

        return jsonify(_empty_trash(database, cfg, folder, delete_raw))

    @app.post("/api/cache/clean")
    def api_cache_clean():
        data = request.get_json(force=True, silent=True) or {}
        folder = request.args.get("folder", "").strip() or data.get("folder", "")
        if folder:
            folder = canonical_path(folder)
            result = cleanup_folder_cache(folder, database, cfg.cache_dir_name)
        else:
            result = _cleanup_cache(database, cfg.cache_dir_name)
        return jsonify(result)

    @app.post("/api/favorite")
    def api_favorite():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id")
        if pid is None:
            return jsonify({"error": "缺少照片 id"}), 400
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return jsonify({"error": "照片 id 不合法"}), 400
        raw_favorite = data.get("favorite", True)
        favorite = (
            raw_favorite
            if isinstance(raw_favorite, bool)
            else str(raw_favorite).strip().lower() in ("1", "true", "yes", "on")
        )
        photo = database.get_photo(pid)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        database.set_favorite(pid, favorite)
        photo["favorite"] = favorite
        return jsonify(_photo_payload(photo))

    @app.post("/api/reanalyze")
    def api_reanalyze():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id")
        if pid is None:
            return jsonify({"error": "缺少照片 id"}), 400
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return jsonify({"error": "照片 id 不合法"}), 400
        try:
            photo = reanalyze_photo(pid, database, cfg)
            return jsonify(_photo_payload(photo))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500

    return app
