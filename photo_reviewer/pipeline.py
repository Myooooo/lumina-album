"""Single-photo processing pipeline shared by scan and re-analyze flows.

This module intentionally has no Flask or job-management dependencies: it
turns one image file into a database record and a model result.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import livephoto, video
from .api_client import analyze_image, build_analysis_context
from .cache import ensure_cache_dirs
from .config import Config
from .exif import extract_exif
from .geocode import geocoding_enabled, reverse_geocode
from .thumbnailer import RAW_EXTENSIONS, make_proxy
from .video import video_cache_key


class AnalysisGate:
    """Limits the number of concurrent local-model calls process-wide."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = 0

    @contextmanager
    def slot(self, limit: int) -> Iterator[None]:
        limit = max(1, int(limit or 1))
        with self._condition:
            while self._active >= limit:
                self._condition.wait()
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify()


ANALYSIS_GATE = AnalysisGate()


# Fields that should survive an index refresh even though the index phase
# itself does not recompute them.
PRESERVED_FIELDS = (
    "score",
    "recommendation",
    "tags",
    "reason",
    "title",
    "model",
    "dimensions",
    "location",
    "exif",
    "analyzed_at",
    "favorite",
    "error",
    # Derived video files outlive a re-index: regenerating a 40 MB remux just
    # because the row was rewritten would be wasteful, and the cache key
    # already pins them to the file's mtime and size.
    "clip_path",
    "play_path",
    "play_state",
    "live_path",
)


@dataclass
class PreparedPhoto:
    """Proxy + lightweight metadata for one image, before LLM analysis."""

    image_path: str
    folder: str
    filename: str
    size: int
    width: int
    height: int
    proxy_path: str
    thumb_path: str
    phash: str
    exif: dict = field(default_factory=dict)
    location: str | None = None
    #: Same-named camera-raw sibling, kept for the "RAW" badge in the UI.
    raw_path: str | None = None
    #: True when ``image_path`` itself is the raw file (no JPEG counterpart).
    is_raw: bool = False
    #: ``photo`` or ``video``.
    media_kind: str = "photo"
    #: Runtime in milliseconds, for videos.
    duration_ms: int | None = None
    #: Codec/bitrate/container details, for videos.
    video_info: dict = field(default_factory=dict)
    #: The video hidden inside a motion photo, or the MOV beside a Live Photo.
    live_path: str | None = None


def prepare_proxy(
    image_path: str, folder: str, config: Config, raw_path: str | None = None
) -> PreparedPhoto:
    """Generate (or reuse) the proxy file for a photo.

    ``raw_path`` records a same-named camera-raw sibling (``DSC_0001.NEF`` next
    to ``DSC_0001.JPG``). The JPEG stays the analysed image; the raw file is
    only remembered so the UI can flag the card.
    """
    image_path = os.path.abspath(image_path)
    folder = os.path.abspath(folder)
    cache_dir = ensure_cache_dirs(folder, config.cache_dir_name)
    proxy_path, width, height, phash, thumb_path = make_proxy(
        image_path,
        max_edge=config.proxy_max_edge,
        cache_dir=str(cache_dir),
        quality=config.proxy_quality,
        thumb_size=config.gallery_thumb_size,
        thumb_quality=config.thumb_quality,
    )
    return PreparedPhoto(
        image_path=image_path,
        folder=folder,
        filename=os.path.basename(image_path),
        size=os.path.getsize(image_path),
        width=width,
        height=height,
        proxy_path=proxy_path,
        thumb_path=thumb_path,
        phash=phash,
        raw_path=os.path.abspath(raw_path) if raw_path else None,
        is_raw=Path(image_path).suffix.lower() in RAW_EXTENSIONS,
    )


def attach_live_video(
    prepared: PreparedPhoto, config: Config
) -> PreparedPhoto:
    """Find the video a still carries, and cache it next to the proxies.

    Returns the same object with ``live_path`` filled in when the still is a
    motion photo (the video is inside the JPEG/HEIC) or half of a Live Photo
    pair (a same-named MOV sits beside it). A plain photo is returned untouched,
    so this is safe to call for every still.

    A *paired* movie is only attached when ``merge_live_photos`` is on: with the
    setting off the movie is indexed as its own item, and attaching it here as
    well would put the same footage on screen twice.
    """
    try:
        source = livephoto.detect(prepared.image_path)
    except OSError:
        return prepared
    if not source:
        return prepared
    if source.video_path and not config.merge_live_photos:
        # Split view: ``discover_photo_pairs`` indexes the movie separately.
        return prepared

    if source.embedded:
        cache_dir = ensure_cache_dirs(prepared.folder, config.cache_dir_name)
        base = video_cache_key(prepared.image_path)
        dest = os.path.join(str(cache_dir), f"{base}_motion.mp4")
        if not os.path.exists(dest) and not livephoto.extract_embedded(
            prepared.image_path, source, dest
        ):
            return prepared
        prepared.live_path = dest
    elif source.video_path:
        # An iPhone pair: nothing to extract, the file already exists.
        prepared.live_path = os.path.abspath(source.video_path)
    return prepared


def prepare_video(image_path: str, folder: str, config: Config) -> PreparedPhoto:
    """Index one video: probe it, grab a poster, and remember what it is.

    A video is analysed by nobody - it goes straight to "已收录" with no score -
    but the gallery still needs a picture, so the poster frame is written as an
    ordinary thumbnail. Everything downstream (the grid, the culling, the
    thumbnail endpoint) then treats a video exactly like a photo.

    When ffmpeg is missing the row is still created: the card shows a
    placeholder and the detail dialog explains how to fix it, which beats
    silently skipping the user's videos.
    """
    image_path = os.path.abspath(image_path)
    folder = os.path.abspath(folder)
    cache_dir = ensure_cache_dirs(folder, config.cache_dir_name)
    info = video.probe(image_path, config.ffmpeg_path or None)
    base = video_cache_key(image_path)

    thumb_path = os.path.join(str(cache_dir), f"{base}_thumb.jpg")
    proxy_path = os.path.join(str(cache_dir), f"{base}_proxy.jpg")
    made = False
    if not os.path.exists(proxy_path):
        made = video.make_poster(
            image_path,
            proxy_path,
            video.poster_seek(info, config.video_poster_ratio),
            max_edge=config.proxy_max_edge,
            quality=max(2, min(8, 31 - config.proxy_quality // 5)),
            override_dir=config.ffmpeg_path or None,
        )
    if made and not os.path.exists(thumb_path):
        video.make_poster(
            image_path,
            thumb_path,
            video.poster_seek(info, config.video_poster_ratio),
            max_edge=config.gallery_thumb_size,
            quality=6,
            override_dir=config.ffmpeg_path or None,
        )

    width = info.width if info else 0
    height = info.height if info else 0
    exif: dict = {}
    if info:
        if info.capture_time:
            exif["datetime_original"] = info.capture_time
        if info.make:
            exif["make"] = info.make
        if info.model:
            exif["model"] = info.model
        if info.latitude is not None and info.longitude is not None:
            exif["latitude"] = info.latitude
            exif["longitude"] = info.longitude

    prepared = PreparedPhoto(
        image_path=image_path,
        folder=folder,
        filename=os.path.basename(image_path),
        size=os.path.getsize(image_path),
        width=width,
        height=height,
        proxy_path=proxy_path if os.path.exists(proxy_path) else "",
        thumb_path=thumb_path if os.path.exists(thumb_path) else "",
        phash="",
        exif=exif,
        location=None,
        media_kind="video",
        duration_ms=info.duration_ms if info else None,
        video_info=info.as_dict() if info else {},
    )
    return prepared


def is_coordinate_location(value) -> bool:
    """Return True when a location string looks like raw lat,lon coordinates."""
    text = str(value or "").strip()
    if not text or "," not in text:
        return False
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2:
        return False
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def enrich_metadata(
    prepared: PreparedPhoto,
    config: Config,
    existing_location: str | None = None,
    already_geocoded: bool = False,
) -> PreparedPhoto:
    """Extract EXIF and resolve a place name when GPS is present.

    The ``geocoded`` flag lives in the ``photos`` table rather than in the file
    (EXIF is re-read from disk on every pass, so a flag written into the parsed
    dict never survives). ``already_geocoded`` carries that persisted state in,
    which is what stops the index and analysis phases from reverse geocoding
    the same photo twice.

    If reverse geocoding fails, a previously resolved human-readable address
    is kept instead of being overwritten by raw coordinates.
    """
    location, exif = extract_exif(prepared.image_path)
    has_gps = exif.get("latitude") is not None and exif.get("longitude") is not None
    if not geocoding_enabled(config.geocoding_provider) and has_gps:
        # The user asked for no reverse geocoding: show the coordinates as they
        # are. An address resolved by an earlier run is deliberately not reused
        # here - "不使用" should be visible immediately, not only for new photos.
        prepared.exif = exif or {}
        prepared.location = location or exif.get("location")
        return prepared
    if already_geocoded:
        # Indexing already resolved this location; avoid a duplicate request.
        # ``extract_exif`` only ever returns raw coordinates, so the resolved
        # address from the database wins when the two disagree.
        if location and is_coordinate_location(location):
            location = existing_location or exif.get("location") or location
        else:
            location = location or existing_location or exif.get("location")
        prepared.exif = exif or {}
    elif exif.get("latitude") is not None and exif.get("longitude") is not None:
        place = reverse_geocode(
            exif["latitude"],
            exif["longitude"],
            config.geocoding_provider,
            config.geocoding_api_key,
            interval=config.geocoding_interval,
            retries=config.geocoding_retries,
        )
        if place:
            location = place
            exif["location"] = location
            exif["geocoded"] = True
        elif existing_location and not is_coordinate_location(existing_location):
            location = existing_location
        prepared.exif = exif or {}
    else:
        if location is None and existing_location:
            location = existing_location
        prepared.exif = exif or {}
    prepared.location = location
    return prepared


def prepare_for_analysis(
    image_path: str,
    folder: str,
    config: Config,
    existing_location: str | None = None,
    raw_path: str | None = None,
    already_geocoded: bool = False,
) -> PreparedPhoto:
    """Prepare proxy + metadata, then return an analysis-ready object."""
    return enrich_metadata(
        prepare_proxy(image_path, folder, config, raw_path=raw_path),
        config,
        existing_location,
        already_geocoded=already_geocoded,
    )


def prepared_from_row(photo: dict) -> PreparedPhoto:
    """Rebuild a ``PreparedPhoto`` from a database row.

    Used when the picture to send to the model is already on disk and does not
    need generating: a video's poster frame, or a re-analysis of a row whose
    proxy is still valid. Everything the analysis needs is in the row, so this
    is a pure reshuffle - no file is touched.
    """
    return PreparedPhoto(
        image_path=photo.get("path") or photo.get("original_path") or "",
        folder=photo.get("folder") or "",
        filename=photo.get("filename") or "",
        size=photo.get("size") or 0,
        width=photo.get("width") or 0,
        height=photo.get("height") or 0,
        proxy_path=photo.get("proxy_path") or "",
        thumb_path=photo.get("thumb_path") or "",
        phash=photo.get("phash") or "",
        exif=photo.get("exif") or {},
        location=photo.get("location"),
        raw_path=photo.get("raw_path"),
        is_raw=Path(photo.get("path") or "").suffix.lower() in RAW_EXTENSIONS,
        media_kind=photo.get("media_kind") or "photo",
        duration_ms=photo.get("duration_ms"),
        video_info=photo.get("video_info") or {},
        live_path=photo.get("live_path"),
    )


def base_record(
    prepared: PreparedPhoto,
    existing: dict | None = None,
    status: str = "pending",
) -> dict:
    """Build the DB record for the indexing phase."""
    record = {
        "path": prepared.image_path,
        "folder": prepared.folder,
        "filename": prepared.filename,
        "size": prepared.size,
        "width": prepared.width,
        "height": prepared.height,
        "thumb_path": prepared.thumb_path,
        "proxy_path": prepared.proxy_path,
        "phash": prepared.phash,
        "raw_path": prepared.raw_path,
        "status": status,
        "media_kind": prepared.media_kind or "photo",
        "duration_ms": prepared.duration_ms,
        "video_info": prepared.video_info or {},
        "live_path": prepared.live_path,
    }
    if existing:
        for key in PRESERVED_FIELDS:
            if existing.get(key) is not None:
                record[key] = existing[key]
    return record


def analysis_record(
    prepared: PreparedPhoto,
    result: dict,
    config: Config,
    existing: dict | None = None,
) -> dict:
    """Build the DB record for a completed model analysis."""
    record = base_record(prepared, existing=existing, status="analyzed")
    record.update(
        {
            "dimensions": result.get("dimensions", {}),
            "location": prepared.location,
            "exif": prepared.exif,
            "score": result["score"],
            "recommendation": result.get("recommendation"),
            "tags": result["tags"],
            "reason": result.get("reason") or result.get("comment"),
            "title": result.get("title") or "",
            "model": config.model,
            "analyzed_at": None,
            "error": None,
        }
    )
    return record


def analyze_prepared(prepared: PreparedPhoto, config: Config) -> dict:
    """Call the local vision model for one prepared photo."""
    with ANALYSIS_GATE.slot(config.scan_concurrency):
        return analyze_image(
            prepared.proxy_path,
            config,
            context=build_analysis_context(prepared.location, prepared.exif),
        )


def persist_analysis(db, prepared: PreparedPhoto, result: dict, config: Config) -> int:
    """Write a completed analysis back to SQLite."""
    existing = db.get_photo_by_path(prepared.image_path)
    record = analysis_record(prepared, result, config, existing=existing)
    photo_id = db.upsert_photo(record)
    db.update_analysis(
        photo_id,
        score=result["score"],
        recommendation=result.get("recommendation"),
        tags=result["tags"],
        reason=result.get("reason") or result.get("comment"),
        model=config.model,
        title=result.get("title") or "",
        phash=prepared.phash,
        width=prepared.width,
        height=prepared.height,
        thumb_path=prepared.thumb_path,
        proxy_path=prepared.proxy_path,
        dimensions=result.get("dimensions", {}),
        location=prepared.location,
        exif=prepared.exif,
        status="analyzed",
    )
    return photo_id
