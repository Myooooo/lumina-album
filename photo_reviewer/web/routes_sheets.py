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

from ..duplicates import (
    DEFAULT_THRESHOLD,
    group_duplicates,
    summarise,
)

from ..exporter import (
    build_html,
    build_tall_image,
    rows_to_export,
    tall_image_bytes,
)

from ..footprints import group_by_city

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

        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M")
        if fmt == "html":
            document = build_html(
                photos,
                title=title,
                subtitle=subtitle,
                colophon="由拾光相册整理",
            )
            payload = document.encode("utf-8")
            return Response(
                payload,
                # Not "text/html; charset=utf-8": Flask appends the charset to a
                # text mimetype, and passing it too produced the doubled header.
                mimetype="text/html",
                headers={
                    "Content-Disposition":
                        f'attachment; filename="lumina-{stamp}.html"',
                    "Content-Length": str(len(payload)),
                    **export_headers,
                },
            )

        image = build_tall_image(photos, title=title, subtitle=subtitle)
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

    @app.get("/api/footprints")
    def api_footprints():
        """Where the photos were taken, grouped into places rather than addresses.

        The reverse geocoder stores full street addresses, which is right for a
        photo's caption and useless for a footprint list - every photo of one park
        would be its own entry. ``city_of`` collapses them to the city, which took
        404 distinct addresses down to 22 places on this library.

        No map tiles: a coordinate map needs a tile server and an internet
        connection, and this app is deliberately local. The place list plus a
        representative photo says the same thing without either.
        """
        folder = request.args.get("folder", "").strip() or None
        rows = database.located_photos(folder)
        footprints = group_by_city(rows)
        return jsonify(
            {
                "total": sum(item.count for item in footprints),
                "places": [
                    {
                        "city": item.city,
                        "count": item.count,
                        "years": item.years,
                        # Only what the cover thumbnail needs: a full photo payload
                        # carries EXIF, tags and captions the place list never shows.
                        "cover": {
                            "id": item.photos[0]["id"],
                            "filename": item.photos[0].get("filename") or "",
                            "thumb_version": item.photos[0].get("phash") or "",
                        },
                        # The place opens to its own grid, so the rows come with it
                        # rather than needing a second request per click. Kept to
                        # the fields a thumbnail and the dialog's first paint read:
                        # the full payload is thirty fields, and a place with 832
                        # photos turned that into a two-megabyte response.
                        "photos": [
                            {
                                "id": photo["id"],
                                "filename": photo.get("filename") or "",
                                # The dialog draws a path row and a RAW marker from
                                # these; the trimmed row used to omit them, so a
                                # photo opened here showed neither.
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
                                "thumb_version": photo.get("phash") or "",
                            }
                            for photo in item.photos
                        ],
                    }
                    for item in footprints
                ],
            }
        )

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
        raw_threshold = request.args.get("threshold", "").strip()
        try:
            threshold = int(raw_threshold) if raw_threshold else DEFAULT_THRESHOLD
        except (TypeError, ValueError):
            return jsonify({"error": "threshold 参数不合法"}), 400
        threshold = max(0, min(threshold, 24))

        candidates = database.duplicate_candidates(folder)
        groups = group_duplicates(candidates, threshold=threshold)
        return jsonify(
            {
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
