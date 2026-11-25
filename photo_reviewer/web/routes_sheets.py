"""Exports, footprints, memories and duplicate groups.

Registered by :func:`photo_reviewer.server.create_app`; the route bodies are
grouped by the part of the app they serve.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from flask import Response, jsonify, request

from ..db import photo_capture_year

from .. import trips as trips_mod
from .. import vectors
from ..duplicates import (
    DEFAULT_THRESHOLD,
    group_duplicates,
    group_duplicates_vector,
    summarise,
)
from ..faces import decode_embedding

from ..exporter import (
    build_html,
    build_tall_image,
    rows_to_export,
    tall_image_bytes,
)

from ..footprints import city_of, merge_places_and_footprints, photo_coords
from ..geocode import forward_geocode, geocoding_enabled

from .constants import (
    MAX_EXPORT_PHOTOS,
)

from .helpers import (
    _duplicate_payload,
    _is_raw_file,
    _photo_payload,
)

from pathlib import Path
from datetime import date


def register(app, cfg, database):
    """Register these routes on ``app``."""

    @app.post("/api/export")
    def api_export():
        """Render a set of photos as a keepsake: one HTML file, or one tall image.

        The download is streamed straight back rather than written to disk - a
        keepsake is something you take away, not something the library has to
        clean up after. ``ids`` is the selection; ``month``/``day`` asks for that
        day's memories instead, so the 那年今日 sheet can export itself.

        Both formats are self-contained: the HTML embeds every picture as a data
        URI and the PNG is a single finished image, so either one still opens in
        ten years with no server, no database and no folder of loose files.
        """
        data = request.get_json(force=True, silent=True) or {}
        fmt = (data.get("format") or "html").strip().lower()
        if fmt not in ("html", "png"):
            return jsonify({"error": "不支持的导出格式"}), 400

        ids = data.get("ids")
        rows: list[dict[str, Any]] = []
        requested = 0
        if ids is not None:
            if not isinstance(ids, list) or not ids:
                return jsonify({"error": "请至少选择一张照片"}), 400
            requested = len(ids)
            for raw in ids[:MAX_EXPORT_PHOTOS]:
                try:
                    pid = int(raw)
                except (TypeError, ValueError):
                    continue
                row = database.get_photo(pid)
                if row and row.get("status") != "deleted":
                    rows.append(row)
        else:
            folder = data.get("folder", "").strip() or None
            today = datetime.now().astimezone()
            try:
                month = int(data.get("month", today.month))
                day = int(data.get("day", today.day))
            except (TypeError, ValueError):
                return jsonify({"error": "month/day 参数不合法"}), 400
            try:
                date(2000, month, day)
            except ValueError:
                return jsonify({"error": "这个日期不存在"}), 400
            window = data.get("window", cfg.memory_window_days)
            try:
                window = int(window)
            except (TypeError, ValueError):
                return jsonify({"error": "window 参数不合法"}), 400
            rows = database.memories_on(
                month, day, window=max(0, min(window, 30)), folder=folder
            )
            # The day's memories have to be held to the same ceiling as a manual
            # selection: both are embedded inline, so an unusually rich date
            # would otherwise build a document nobody can open.
            requested = len(rows)
            rows = rows[:MAX_EXPORT_PHOTOS]

        if not rows:
            return jsonify({"error": "没有可以导出的照片"}), 400

        title = (data.get("title") or "拾光回忆").strip()[:80]
        subtitle = (data.get("subtitle") or "").strip()[:80]
        photos = rows_to_export(rows)
        # Say out loud how much of the selection made it into the file. A
        # keepsake that quietly dropped the last hundred photos reads as a bug
        # in the download, not as a limit, so the headers travel back with it
        # and the client warns when the two numbers disagree.
        included = len(photos)
        export_headers = {
            "X-Export-Requested": str(requested),
            "X-Export-Included": str(included),
            "X-Export-Limit": str(MAX_EXPORT_PHOTOS),
            "X-Export-Truncated": "1" if included < requested else "0",
        }

        theme = (data.get("theme") or "paper").strip().lower()
        if theme not in ("paper", "classic", "vintage"):
            theme = "paper"
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M")
        if fmt == "html":
            document = build_html(
                photos,
                title=title,
                subtitle=subtitle,
                colophon="由拾光相册整理",
                theme=theme,
            )
            payload = document.encode("utf-8")
            return Response(
                payload,
                mimetype="text/html",
                headers={
                    "Content-Disposition":
                        f'attachment; filename="lumina-{stamp}.html"',
                    "Content-Length": str(len(payload)),
                    **export_headers,
                },
            )

        image = build_tall_image(photos, title=title, subtitle=subtitle, theme=theme)
        if image is None:
            return jsonify({"error": "没有可以导出的照片"}), 400
        try:
            payload = tall_image_bytes(image)
        finally:
            image.close()
        return Response(
            payload,
            mimetype="image/png",
            headers={
                "Content-Disposition": f'attachment; filename="lumina-{stamp}.png"',
                "Content-Length": str(len(payload)),
                **export_headers,
            },
        )

    def _serialize_footprint_photo(photo):
        coords = photo_coords(photo)
        return {
            "id": photo["id"],
            "filename": photo.get("filename") or "",
            "path": photo.get("path") or "",
            "raw_path": photo.get("raw_path"),
            "has_raw": bool(photo.get("raw_path")),
            "is_raw": _is_raw_file(
                photo.get("path") or photo.get("original_path")
            ),
            "raw_ext": (
                Path(photo["raw_path"]).suffix.lstrip(".").upper()
                if photo.get("raw_path")
                else ""
            ),
            "size": photo.get("size") or 0,
            "width": photo.get("width") or 0,
            "height": photo.get("height") or 0,
            "status": photo.get("status") or "",
            "media_kind": photo.get("media_kind") or "photo",
            "score": photo.get("score"),
            "favorite": bool(photo.get("favorite")),
            "location": photo.get("location"),
            "place_id": photo.get("place_id"),
            "lat": coords[0] if coords else None,
            "lon": coords[1] if coords else None,
            "thumb_version": photo.get("phash") or "",
        }

    @app.get("/api/footprints")
    def api_footprints():
        """Return unified footprints combining user-created places and EXIF cities.

        Passing ``summary=1`` omits the full per-place ``photos`` array for large
        places and returns up to 12 ``preview_photos`` per place so large libraries
        load instantaneously; callers then fetch full photos on demand via
        ``GET /api/footprints/photos``.
        """
        folder = request.args.get("folder", "").strip() or None
        summary_only = request.args.get("summary", "0").strip().lower() in ("1", "true", "yes")
        rows = database.located_photos(folder)
        custom_places = database.list_places()
        footprints = merge_places_and_footprints(rows, custom_places)

        def _cover_for(item):
            if not item.photos:
                return None
            chosen = None
            if item.cover_photo_id:
                chosen = next((p for p in item.photos if p["id"] == item.cover_photo_id), None)
            if chosen is None:
                chosen = item.photos[0]
            return {
                "id": chosen["id"],
                "filename": chosen.get("filename") or "",
                "thumb_version": chosen.get("phash") or "",
            }

        active_coords = [
            (item.lat, item.lon)
            for item in footprints
            if item.count > 0 and item.lat is not None and item.lon is not None
        ]
        bounds = None
        if active_coords:
            bounds = {
                "min_lat": min(c[0] for c in active_coords),
                "max_lat": max(c[0] for c in active_coords),
                "min_lon": min(c[1] for c in active_coords),
                "max_lon": max(c[1] for c in active_coords),
            }

        places_out = []
        for item in footprints:
            key = f"place:{item.place_id}" if item.place_id is not None else f"city:{item.city}"
            entry = {
                "key": key,
                "id": item.place_id,
                "name": item.name,
                "city": item.city,
                "address": item.address,
                "source": item.source,
                "lat": item.lat,
                "lon": item.lon,
                "count": item.count,
                "years": item.years,
                "cover": _cover_for(item),
            }
            if summary_only:
                preview = [_serialize_footprint_photo(p) for p in item.photos[:12]]
                entry["photos_loaded"] = item.count <= 12
                entry["preview_photos"] = preview
                entry["photos"] = preview if item.count <= 12 else []
            else:
                entry["photos_loaded"] = True
                entry["photos"] = [_serialize_footprint_photo(p) for p in item.photos]
            places_out.append(entry)

        return jsonify(
            {
                "total": sum(item.count for item in footprints),
                "bounds": bounds,
                "places": places_out,
            }
        )

    @app.get("/api/footprints/photos")
    def api_footprint_place_photos():
        """Fetch paginated photos for one footprint place/city on demand."""
        key = request.args.get("key", "").strip()
        if not key:
            return jsonify({"error": "缺少地点标识 key"}), 400
        folder = request.args.get("folder", "").strip() or None
        try:
            offset = max(0, int(request.args.get("offset", "0")))
            limit = max(1, min(2000, int(request.args.get("limit", "500"))))
        except (TypeError, ValueError):
            return jsonify({"error": "分页参数不合法"}), 400

        rows = database.located_photos(folder)
        custom_places = database.list_places()
        footprints = merge_places_and_footprints(rows, custom_places)
        target = None
        for item in footprints:
            item_key = f"place:{item.place_id}" if item.place_id is not None else f"city:{item.city}"
            if item_key == key:
                target = item
                break
        if target is None:
            return jsonify({"key": key, "photos": [], "total": 0, "offset": offset, "limit": limit, "has_more": False})

        total = len(target.photos)
        slice_rows = target.photos[offset : offset + limit]
        return jsonify(
            {
                "key": key,
                "photos": [_serialize_footprint_photo(p) for p in slice_rows],
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(slice_rows) < total,
            }
        )

    @app.get("/api/places")
    def api_list_places():
        return jsonify({"places": database.list_places()})

    @app.post("/api/places/geocode")
    def api_places_geocode():
        data = request.get_json(force=True, silent=True) or {}
        query = str(data.get("query") or "").strip()
        if not query:
            return jsonify({"error": "请输入要查询的地点名称"}), 400
        if not geocoding_enabled(cfg.geocoding_provider):
            return jsonify(
                {
                    "candidates": [],
                    "provider": cfg.geocoding_provider,
                    "message": "当前已在「设置 → 地点」中关闭外部地理编码服务，可手动填写经纬度。",
                }
            )
        candidates = forward_geocode(
            query,
            provider=cfg.geocoding_provider,
            api_key=cfg.geocoding_api_key,
            interval=cfg.geocoding_interval,
            retries=cfg.geocoding_retries,
            allow_fallback=cfg.geocoding_fallback,
            limit=5,
        )
        return jsonify(
            {
                "candidates": candidates,
                "provider": cfg.geocoding_provider,
            }
        )

    @app.post("/api/places")
    def api_create_place():
        data = request.get_json(force=True, silent=True) or {}
        name = str(data.get("name") or "").strip()[:80]
        if not name:
            return jsonify({"error": "请输入地点名称"}), 400
        if database.find_place_by_name(name):
            return jsonify({"error": f"已存在名为「{name}」的地点"}), 409

        address = str(data.get("address") or "").strip()[:200]
        city = str(data.get("city") or "").strip()[:80]
        lat = data.get("lat")
        lon = data.get("lon")
        try:
            lat = float(lat) if lat not in (None, "") else None
            lon = float(lon) if lon not in (None, "") else None
        except (TypeError, ValueError):
            return jsonify({"error": "经纬度格式不正确"}), 400
        if lat is not None and not (-90.0 <= lat <= 90.0):
            return jsonify({"error": "纬度需在 -90 到 90 之间"}), 400
        if lon is not None and not (-180.0 <= lon <= 180.0):
            return jsonify({"error": "经度需在 -180 到 180 之间"}), 400

        geocoded = False
        if (lat is None or lon is None) and data.get("auto_geocode", True) and geocoding_enabled(cfg.geocoding_provider):
            cands = forward_geocode(
                address or name,
                provider=cfg.geocoding_provider,
                api_key=cfg.geocoding_api_key,
                interval=cfg.geocoding_interval,
                retries=cfg.geocoding_retries,
                allow_fallback=cfg.geocoding_fallback,
                limit=1,
            )
            if cands:
                lat = cands[0]["lat"]
                lon = cands[0]["lon"]
                if not address:
                    address = cands[0].get("address") or name
                if not city:
                    city = cands[0].get("city") or city_of(address) or name
                geocoded = True

        if not city:
            city = city_of(address or name) or name

        place_id = database.create_place(
            name=name,
            city=city,
            address=address or name,
            lat=lat,
            lon=lon,
            source="manual",
        )
        raw_photo_ids = data.get("photo_ids") or []
        assigned = 0
        if isinstance(raw_photo_ids, list) and raw_photo_ids:
            valid_ids = []
            for pid in raw_photo_ids:
                try:
                    valid_ids.append(int(pid))
                except (TypeError, ValueError):
                    continue
            if valid_ids:
                assigned = database.assign_photos_to_place(place_id, valid_ids)
        return jsonify(
            {
                "ok": True,
                "place": database.get_place(place_id),
                "geocoded": geocoded,
                "assigned": assigned,
            }
        ), 201

    @app.patch("/api/places/<int:place_id>")
    def api_update_place(place_id: int):
        existing = database.get_place(place_id)
        if not existing:
            return jsonify({"error": "地点不存在"}), 404
        data = request.get_json(force=True, silent=True) or {}
        name = None
        if "name" in data:
            name = str(data.get("name") or "").strip()[:80]
            if not name:
                return jsonify({"error": "地点名称不能为空"}), 400
            if database.find_place_by_name(name, exclude_id=place_id):
                return jsonify({"error": f"已存在名为「{name}」的地点"}), 409
        address = str(data.get("address") or "").strip()[:200] if "address" in data else None
        city = str(data.get("city") or "").strip()[:80] if "city" in data else None
        update_coords = "lat" in data or "lon" in data
        lat = existing.get("lat")
        lon = existing.get("lon")
        if update_coords:
            try:
                lat = float(data["lat"]) if data.get("lat") not in (None, "") else None
                lon = float(data["lon"]) if data.get("lon") not in (None, "") else None
            except (TypeError, ValueError):
                return jsonify({"error": "经纬度格式不正确"}), 400
            if lat is not None and not (-90.0 <= lat <= 90.0):
                return jsonify({"error": "纬度需在 -90 到 90 之间"}), 400
            if lon is not None and not (-180.0 <= lon <= 180.0):
                return jsonify({"error": "经度需在 -180 到 180 之间"}), 400
        update_cover = "cover_photo_id" in data
        cover_photo_id = None
        if update_cover and data.get("cover_photo_id") is not None:
            try:
                cover_photo_id = int(data["cover_photo_id"])
            except (TypeError, ValueError):
                return jsonify({"error": "cover_photo_id 不合法"}), 400
        updated = database.update_place(
            place_id,
            name=name,
            city=city,
            address=address,
            lat=lat,
            lon=lon,
            update_coords=update_coords,
            cover_photo_id=cover_photo_id,
            update_cover=update_cover,
        )
        return jsonify({"ok": True, "place": updated})

    @app.delete("/api/places/<int:place_id>")
    def api_delete_place(place_id: int):
        if not database.get_place(place_id):
            return jsonify({"error": "地点不存在"}), 404
        database.delete_place(place_id, clear_location=True)
        return jsonify({"ok": True})

    @app.post("/api/places/<int:place_id>/photos")
    def api_add_place_photos(place_id: int):
        if not database.get_place(place_id):
            return jsonify({"error": "地点不存在"}), 404
        data = request.get_json(force=True, silent=True) or {}
        raw_ids = data.get("photo_ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify({"error": "请选择至少一张照片"}), 400
        valid_ids = []
        for pid in raw_ids:
            try:
                valid_ids.append(int(pid))
            except (TypeError, ValueError):
                continue
        if not valid_ids:
            return jsonify({"error": "照片 ID 不合法"}), 400
        updated = database.assign_photos_to_place(place_id, valid_ids)
        return jsonify({"ok": True, "updated": updated, "place": database.get_place(place_id)})

    @app.delete("/api/places/<int:place_id>/photos")
    def api_remove_place_photos(place_id: int):
        if not database.get_place(place_id):
            return jsonify({"error": "地点不存在"}), 404
        data = request.get_json(force=True, silent=True) or {}
        raw_ids = data.get("photo_ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify({"error": "请选择要移除的照片"}), 400
        valid_ids = []
        for pid in raw_ids:
            try:
                valid_ids.append(int(pid))
            except (TypeError, ValueError):
                continue
        removed = database.remove_photos_from_place(place_id, valid_ids)
        return jsonify({"ok": True, "removed": removed, "place": database.get_place(place_id)})

    @app.get("/api/memories")
    def api_memories():
        """Photos taken on this day in earlier years.

        Grouped by year so the page can present them as "一年前的今天" and so on -
        a flat list of forty photos from four years loses the thing that makes a
        memory a memory. The window comes from the settings (0 = the exact day),
        and ``day``/``month`` can be overridden so the page works for a date the
        user picked as well as for today.
        """
        folder = request.args.get("folder", "").strip() or None
        today = datetime.now().astimezone()
        try:
            month = int(request.args.get("month", today.month))
            day = int(request.args.get("day", today.day))
        except (TypeError, ValueError):
            return jsonify({"error": "month/day 参数不合法"}), 400
        try:
            date(2000, month, day)
        except ValueError:
            return jsonify({"error": "这个日期不存在"}), 400

        window = cfg.memory_window_days
        if "window" in request.args:
            try:
                window = int(request.args.get("window"))
            except (TypeError, ValueError):
                return jsonify({"error": "window 参数不合法"}), 400
        window = max(0, min(window, 30))

        rows = database.memories_on(month, day, window=window, folder=folder)
        buckets: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            year = photo_capture_year(row)
            if not year:
                continue
            buckets.setdefault(year, []).append(row)

        years = []
        for year in sorted(buckets, reverse=True):
            photos = buckets[year]
            years.append(
                {
                    "year": year,
                    "years_ago": today.year - year,
                    "count": len(photos),
                    "photos": [_photo_payload(p) for p in photos],
                }
            )
        return jsonify(
            {
                "month": month,
                "day": day,
                "window": window,
                "total": sum(item["count"] for item in years),
                "years": years,
            }
        )

    @app.get("/api/duplicates")
    def api_duplicates():
        """Groups of photos that look like the same picture.

        Exposed as its own page rather than as a badge on the gallery: deciding
        what to throw away needs the group in front of you, at a size where the
        difference is visible. Everything is computed from the stored dHash, so
        the request costs a second on an eleven-thousand-photo library and
        touches no files.

        ``threshold`` is the Hamming distance. The default (6) is conservative on
        purpose and the UI offers it as a slider, because "how alike is alike"
        is a judgement call the reviewer should make, not one to bury.
        """
        folder = request.args.get("folder", "").strip() or None
        dup_mode = (request.args.get("mode", "phash") or "phash").strip().lower()
        if dup_mode not in ("phash", "vector"):
            dup_mode = "phash"
        raw_threshold = request.args.get("threshold", "").strip()
        try:
            threshold = int(raw_threshold) if raw_threshold else DEFAULT_THRESHOLD
        except (TypeError, ValueError):
            return jsonify({"error": "threshold 参数不合法"}), 400
        threshold = max(0, min(threshold, 24))

        candidates = database.duplicate_candidates(folder)
        if dup_mode == "vector":
            model = vectors.active_vector_model(cfg)
            raw_embs = database.list_photo_embeddings(model, photo_ids=[c["id"] for c in candidates])
            embs = {}
            for pid, blob in raw_embs.items():
                vec = decode_embedding(blob)
                if vec:
                    embs[pid] = vec
            # Map dHash threshold slider (0..14) cleanly onto cosine similarity (0.98..0.86)
            min_sim = max(0.84, min(0.985, 0.98 - threshold * 0.008))
            groups = group_duplicates_vector(candidates, embs, min_similarity=min_sim)
        else:
            groups = group_duplicates(candidates, threshold=threshold)
        return jsonify(
            {
                "mode": dup_mode,
                "threshold": threshold,
                "summary": summarise(groups),
                "groups": [
                    {
                        "keep_id": group.keep_id,
                        "wasted_bytes": group.wasted_bytes,
                        "photos": [_duplicate_payload(p) for p in group.photos],
                    }
                    for group in groups
                ],
            }
        )

    @app.get("/api/trips")
    def api_trips():
        """Auto-clustered Trips & Stories for the current folder or library."""
        folder = request.args.get("folder", "").strip() or None
        try:
            max_gap = max(1, min(int(request.args.get("max_gap_days", 3)), 14))
        except (TypeError, ValueError):
            max_gap = 3
        try:
            min_photos = max(2, min(int(request.args.get("min_photos", 3)), 50))
        except (TypeError, ValueError):
            min_photos = 3
        rows = database.list_photos(
            folder=folder, exclude_deleted=True, sort="time_asc", limit=20000
        )
        trip_list = trips_mod.cluster_trips(rows, max_gap_days=max_gap, min_photos=min_photos)
        return jsonify({"trips": trip_list, "total": len(trip_list)})

    @app.get("/api/trips/<trip_id>")
    def api_trip_detail(trip_id: str):
        folder = request.args.get("folder", "").strip() or None
        rows = database.list_photos(
            folder=folder, exclude_deleted=True, sort="time_asc", limit=20000
        )
        trip_list = trips_mod.cluster_trips(rows)
        target = next((t for t in trip_list if t["id"] == trip_id), None)
        if not target:
            return jsonify({"error": "未找到该旅程"}), 404
        by_id = {p["id"]: p for p in rows}
        photos = [_photo_payload(by_id[pid]) for pid in target["photo_ids"] if pid in by_id]
        highlights = [_photo_payload(by_id[pid]) for pid in target["highlight_ids"] if pid in by_id]
        return jsonify({"trip": target, "photos": photos, "highlights": highlights})
