"""Automatic Trips & Stories clustering from capture timestamps and GPS coordinates.

Groups chronologically contiguous photos into multi-day trips/outings, builds a
deduplicated chronological route polyline for the Footprints map, and selects
score-ranked, temporally balanced highlight shots for keepsake export.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import Any

from .db import parse_capture_datetime
from .footprints import city_of, photo_coords


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS-84 points in kilometres."""
    r = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(max(0.0, a))))


def _photo_dt(photo: dict[str, Any]) -> datetime | None:
    raw = photo.get("captured_at")
    if not raw:
        exif = photo.get("exif")
        if isinstance(exif, dict):
            raw = (
                exif.get("datetime_original")
                or exif.get("DateTimeOriginal")
                or exif.get("datetime")
                or exif.get("create_date")
            )
    if not raw:
        return None
    return parse_capture_datetime(raw)


def select_trip_highlights(
    photos: list[dict[str, Any]], max_count: int = 16
) -> list[dict[str, Any]]:
    """Pick up to ``max_count`` representative high-scoring photos across a trip.

    Divides the trip into temporal buckets so highlights cover the whole journey
    rather than clustering in a single burst, and prefers higher AI scores and
    favourites.
    """
    if len(photos) <= max_count:
        return list(photos)
    buckets = max(1, min(max_count, len(photos)))
    step = len(photos) / buckets
    picked: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for i in range(buckets):
        start = int(i * step)
        end = max(start + 1, int((i + 1) * step))
        window = photos[start:end]
        best = max(
            window,
            key=lambda p: (
                1 if p.get("favorite") else 0,
                float(p["score"]) if p.get("score") is not None else -1.0,
                -int(p["id"]),
            ),
        )
        if best["id"] not in seen_ids:
            seen_ids.add(best["id"])
            picked.append(best)
    return picked[:max_count]


def _build_route(photos: list[dict[str, Any]], min_step_km: float = 1.5) -> list[dict[str, Any]]:
    """Build a chronological list of deduplicated GPS waypoints for map polylines."""
    route: list[dict[str, Any]] = []
    for photo in photos:
        coords = photo_coords(photo)
        if not coords:
            continue
        lat, lon = coords
        if route:
            prev = route[-1]
            if haversine_km(prev["lat"], prev["lon"], lat, lon) < min_step_km:
                continue
        label = city_of(photo.get("location") or "") if photo.get("location") else ""
        route.append(
            {
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "label": label or (photo.get("location") or ""),
                "photo_id": photo["id"],
            }
        )
    return route


def _trip_title(start: datetime, end: datetime, cities: list[str]) -> str:
    days = (end.date() - start.date()).days + 1
    ym = f"{start.year}.{start.month:02d}"
    if cities:
        place = " · ".join(cities[:2])
        if days >= 2:
            return f"{ym} · {place} {days} 日"
        return f"{ym} · {place}"
    if days >= 2:
        return f"{ym} · {days} 日旅程"
    return f"{start.year}.{start.month:02d}.{start.day:02d} 片段"


def cluster_trips(
    photos: list[dict[str, Any]],
    max_gap_days: int = 3,
    min_photos: int = 3,
    jump_split_km: float = 250.0,
) -> list[dict[str, Any]]:
    """Cluster chronologically ordered photos into Trips & Stories.

    Returns newest trips first. Each trip dict includes ``id``, ``title``,
    ``start_date``, ``end_date``, ``days``, ``cities``, ``route``, ``photo_count``,
    ``cover_photo_id``, ``highlight_ids``, and ``photo_ids``.
    """
    timed: list[tuple[datetime, dict[str, Any]]] = []
    for photo in photos:
        dt = _photo_dt(photo)
        if dt is not None:
            timed.append((dt, photo))
    timed.sort(key=lambda item: (item[0], int(item[1]["id"])))

    if not timed:
        return []

    raw_clusters: list[list[tuple[datetime, dict[str, Any]]]] = [[timed[0]]]
    last_coords = photo_coords(timed[0][1])

    for dt, photo in timed[1:]:
        prev_dt, _prev_photo = raw_clusters[-1][-1]
        gap_days = (dt.date() - prev_dt.date()).days
        cur_coords = photo_coords(photo)
        big_jump = False
        if gap_days >= 1 and last_coords and cur_coords:
            if haversine_km(last_coords[0], last_coords[1], cur_coords[0], cur_coords[1]) >= jump_split_km:
                big_jump = True
        if gap_days > max_gap_days or big_jump:
            raw_clusters.append([(dt, photo)])
        else:
            raw_clusters[-1].append((dt, photo))
        if cur_coords:
            last_coords = cur_coords

    trips: list[dict[str, Any]] = []
    for cluster in raw_clusters:
        if len(cluster) < min_photos:
            continue
        start_dt = cluster[0][0]
        end_dt = cluster[-1][0]
        members = [p for _dt, p in cluster]
        cities: list[str] = []
        seen_cities: set[str] = set()
        for p in members:
            loc = (p.get("location") or "").strip()
            if not loc:
                continue
            c = city_of(loc)
            if c and c != "其他地方" and c not in seen_cities:
                seen_cities.add(c)
                cities.append(c)

        highlights = select_trip_highlights(members, max_count=16)
        cover = max(
            highlights,
            key=lambda p: (
                1 if p.get("favorite") else 0,
                float(p["score"]) if p.get("score") is not None else -1.0,
                -int(p["id"]),
            ),
        )
        route = _build_route(members)
        days = (end_dt.date() - start_dt.date()).days + 1
        title = _trip_title(start_dt, end_dt, cities)
        sig_src = f"{start_dt.strftime('%Y%m%d')}-{end_dt.strftime('%Y%m%d')}-{members[0]['id']}-{len(members)}"
        trip_id = f"trip-{start_dt.strftime('%Y%m%d')}-{hashlib.sha1(sig_src.encode()).hexdigest()[:6]}"
        trips.append(
            {
                "id": trip_id,
                "title": title,
                "start_date": start_dt.strftime("%Y-%m-%d"),
                "end_date": end_dt.strftime("%Y-%m-%d"),
                "days": days,
                "cities": cities,
                "route": route,
                "photo_count": len(members),
                "cover_photo_id": cover["id"],
                "cover_thumb_version": cover.get("phash") or "",
                "highlight_ids": [p["id"] for p in highlights],
                "photo_ids": [p["id"] for p in members],
            }
        )

    trips.sort(key=lambda t: (t["start_date"], t["photo_count"]), reverse=True)
    return trips
