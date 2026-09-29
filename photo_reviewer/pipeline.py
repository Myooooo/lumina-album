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

from .api_client import analyze_image, build_analysis_context
from .cache import ensure_cache_dirs
from .config import Config
from .exif import extract_exif
from .geocode import reverse_geocode
from .thumbnailer import RAW_EXTENSIONS, make_proxy


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
