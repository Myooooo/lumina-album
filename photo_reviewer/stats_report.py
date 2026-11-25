"""Photography Year-in-Review & Shooting Gear Analytics.

Aggregates camera bodies, lenses, focal length buckets, monthly shooting
activity, top cities, top tags, and AI score distributions from SQLite rows.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from .footprints import city_of

_FOCAL_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_focal_mm(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if val > 0 else None
    m = _FOCAL_RE.search(str(raw))
    if not m:
        return None
    try:
        val = float(m.group(1))
        return val if val > 0 else None
    except ValueError:
        return None


def focal_bucket(mm: float) -> str:
    if mm < 24:
        return "超广角 (<24mm)"
    if mm <= 35:
        return "广角 (24-35mm)"
    if mm <= 70:
        return "标准 (36-70mm)"
    if mm <= 135:
        return "中长焦 (71-135mm)"
    return "长焦 (>135mm)"


def build_shooting_report(
    photos: list[dict[str, Any]], year: int | None = None
) -> dict[str, Any]:
    """Build a structured Year-in-Review / Gear statistics payload."""
    available_years: set[int] = set()
    filtered: list[dict[str, Any]] = []

    for p in photos:
        cap = str(p.get("captured_at") or "").strip()
        y_val = None
        if len(cap) >= 4 and cap[:4].isdigit():
            y_val = int(cap[:4])
            available_years.add(y_val)
        if year is not None and y_val != year:
            continue
        filtered.append(p)

    scores = [float(p["score"]) for p in filtered if p.get("score") is not None]
    favorites = sum(1 for p in filtered if p.get("favorite"))
    videos = sum(1 for p in filtered if (p.get("media_kind") or "photo") == "video")
    high_scores = sum(1 for s in scores if s >= 8.5)

    camera_counts: Counter[str] = Counter()
    camera_scores: dict[str, list[float]] = defaultdict(list)
    lens_counts: Counter[str] = Counter()
    lens_scores: dict[str, list[float]] = defaultdict(list)
    focal_counts: Counter[str] = Counter()
    month_counts: Counter[str] = Counter()
    city_counts: Counter[str] = Counter()
    tag_counts: Counter[str] = Counter()

    for p in filtered:
        exif = p.get("exif") if isinstance(p.get("exif"), dict) else {}
        cam = str(exif.get("model") or exif.get("camera") or "").strip()
        lens = str(exif.get("lens_model") or exif.get("lens") or "").strip()
        sc = float(p["score"]) if p.get("score") is not None else None

        if cam:
            camera_counts[cam] += 1
            if sc is not None:
                camera_scores[cam].append(sc)
        if lens:
            lens_counts[lens] += 1
            if sc is not None:
                lens_scores[lens].append(sc)

        mm = parse_focal_mm(
            exif.get("focal_length_35mm")
            or exif.get("focal_length")
            or exif.get("FocalLength")
        )
        if mm is not None:
            focal_counts[focal_bucket(mm)] += 1

        cap = str(p.get("captured_at") or "").strip()
        if len(cap) >= 7 and cap[:4].isdigit() and cap[5:7].isdigit():
            month_key = cap[5:7] if year is not None else cap[:7]
            month_counts[month_key] += 1

        loc = str(p.get("location") or "").strip()
        if loc:
            c = city_of(loc)
            if c and c != "其他地方":
                city_counts[c] += 1

        for t in p.get("tags") or []:
            ts = str(t).strip()
            if ts:
                tag_counts[ts] += 1

    def _gear_rows(counts: Counter[str], score_map: dict[str, list[float]]) -> list[dict[str, Any]]:
        rows = []
        for name, cnt in counts.most_common(6):
            sc_list = score_map.get(name) or []
            avg = round(sum(sc_list) / len(sc_list), 2) if sc_list else None
            rows.append({"name": name, "count": cnt, "avg_score": avg})
        return rows

    focal_order = [
        "超广角 (<24mm)",
        "广角 (24-35mm)",
        "标准 (36-70mm)",
        "中长焦 (71-135mm)",
        "长焦 (>135mm)",
    ]
    focal_rows = [
        {"bucket": b, "count": focal_counts.get(b, 0)}
        for b in focal_order
        if focal_counts.get(b, 0) > 0
    ]

    if year is not None:
        months = [
            {"label": f"{m:02d}月", "count": month_counts.get(f"{m:02d}", 0)}
            for m in range(1, 13)
        ]
    else:
        months = [
            {"label": k, "count": month_counts[k]}
            for k in sorted(month_counts.keys())[-12:]
        ]

    return {
        "year": year,
        "available_years": sorted(available_years, reverse=True),
        "summary": {
            "total": len(filtered),
            "photos": len(filtered) - videos,
            "videos": videos,
            "analyzed": len(scores),
            "favorites": favorites,
            "high_scores": high_scores,
            "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
            "cities_count": len(city_counts),
        },
        "cameras": _gear_rows(camera_counts, camera_scores),
        "lenses": _gear_rows(lens_counts, lens_scores),
        "focal_buckets": focal_rows,
        "months": months,
        "top_cities": [{"name": k, "count": v} for k, v in city_counts.most_common(8)],
        "top_tags": [{"name": k, "count": v} for k, v in tag_counts.most_common(10)],
    }
