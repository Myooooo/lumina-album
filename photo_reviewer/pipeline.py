"""Single-photo processing pipeline shared by scan and re-analyze flows.

This module intentionally has no Flask or job-management dependencies: it
turns one image file into a database record and a model result.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from collections.abc import Iterator

from .api_client import analyze_image, build_analysis_context
from .cache import ensure_cache_dirs
from .config import Config
from .exif import extract_exif
from .geocode import reverse_geocode
from .thumbnailer import make_proxy


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


def prepare_proxy(image_path: str, folder: str, config: Config) -> PreparedPhoto:
    """Generate (or reuse) the proxy file for a photo."""
    image_path = os.path.abspath(image_path)
    folder = os.path.abspath(folder)
    cache_dir = ensure_cache_dirs(folder, config.cache_dir_name)
    proxy_path, width, height, phash = make_proxy(
        image_path,
        max_edge=config.proxy_max_edge,
        cache_dir=str(cache_dir),
        quality=config.proxy_quality,
    )
    return PreparedPhoto(
        image_path=image_path,
        folder=folder,
        filename=os.path.basename(image_path),
        size=os.path.getsize(image_path),
        width=width,
        height=height,
        proxy_path=proxy_path,
        thumb_path=proxy_path,
        phash=phash,
    )


def enrich_metadata(prepared: PreparedPhoto, config: Config) -> PreparedPhoto:
    """Extract EXIF data and, when GPS is present, resolve a place name."""
    location, exif = extract_exif(prepared.image_path)
    if exif.get("latitude") is not None and exif.get("longitude") is not None:
        place = reverse_geocode(
            exif["latitude"],
            exif["longitude"],
            config.geocoding_provider,
            config.geocoding_api_key,
        )
        if place:
            location = place
    prepared.location = location
    prepared.exif = exif or {}
    return prepared


def prepare_for_analysis(image_path: str, folder: str, config: Config) -> PreparedPhoto:
    """Prepare proxy + metadata, then return an analysis-ready object."""
    return enrich_metadata(prepare_proxy(image_path, folder, config), config)


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
