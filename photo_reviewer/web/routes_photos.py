"""Gallery list, search, stats and one photo.

Registered by :func:`photo_reviewer.server.create_app`; the route bodies are
grouped by the part of the app they serve.
"""

from __future__ import annotations

from flask import jsonify, request, url_for

from .. import video

from ..api_client import (
    SEMANTIC_CANDIDATE_LIMIT,
    SemanticSearchError,
    probe_endpoint,
    semantic_search,
    visual_search,
)

from ..db import sort_photos

from .constants import (
    SEARCH_MODES,
    SEARCH_PROBE_TIMEOUT,
    VISUAL_SEARCH_BATCH,
    VISUAL_SEARCH_MAX,
    _LEGACY_SEARCH_MODES,
)

from .helpers import (
    _ensure_exif,
    _file_size,
    _photo_payload,
)


def register(app, cfg, database):
    """Register these routes on ``app``."""

    @app.get("/api/photos")
    def api_photos():
        folder = request.args.get("folder", "").strip() or None
        status_raw = request.args.get("status", "").strip()
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
        # A retired mode is redirected rather than rejected: the page is a single
        # long-lived tab, so it can still be holding the old option value.
        mode = _LEGACY_SEARCH_MODES.get(mode, mode)
        if mode not in SEARCH_MODES:
            # An unknown mode used to fall through every branch and return an
            # empty result set, which reads as "nothing matches" rather than
            # "that is not a search mode".
            return jsonify({"error": "不支持的搜索方式"}), 400
        status_raw = data.get("status", "").strip()
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
        # ``semantic_search`` only ever sends the first 500 candidates to the
        # model, so fetching 2000 meant decoding four times as many rows - tags,
        # EXIF, dimensions, video_info - as the prompt could use.
        all_candidates = database.list_photos(
            folder=folder,
            status=status,
            min_score=min_score,
            tag=tag,
            favorite=favorite,
            year=year,
            exclude_deleted=exclude_deleted,
            sort=sort,
            limit=SEMANTIC_CANDIDATE_LIMIT,
        )
        result_map = {}
        visual_checked = 0

        if mode == "smart":
            keyword_photos = database.list_photos(
                folder=folder,
                status=status,
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
        if mode == "smart" and query:
            # One cheap request before the expensive one. Without it a stopped
            # model server costs the whole retry budget before anything is
            # reported - the same reason `/api/analyze/batch` probes first.
            #
            # A short timeout: the reader is watching a spinner, and the common
            # case is a closed port, which the OS refuses in microseconds. The
            # default three seconds is right for a batch run, not for a keystroke.
            reachable, why = probe_endpoint(cfg, timeout=SEARCH_PROBE_TIMEOUT)
            if not reachable:
                # `smart` still has its keyword recall; return that with an
                # explanation rather than making the reader wait for a timeout.
                semantic_warning = why
            else:
                try:
                    ids = semantic_search(query, all_candidates, cfg)
                    by_id = {p["id"]: p for p in all_candidates}
                    for pid in ids:
                        if pid in by_id:
                            result_map[pid] = by_id[pid]
                except SemanticSearchError as exc:
                    semantic_warning = str(exc)

        # Visual search looks at the pictures instead of the captions, so it can
        # find things no caption happens to describe. It costs a real request per
        # few photos, which is why it is its own mode and why the candidate list is
        # narrowed first by whatever filters the caller already set.
        if mode == "visual" and query:
            # This mode *is* the model looking at pictures; there is no local
            # fallback, so an unreachable server is answered at once.
            reachable, why = probe_endpoint(cfg, timeout=SEARCH_PROBE_TIMEOUT)
            if not reachable:
                return jsonify({"error": why, "unreachable": True}), 503
            try:
                ids, checked = visual_search(
                    query,
                    all_candidates,
                    cfg,
                    batch_size=VISUAL_SEARCH_BATCH,
                    max_candidates=VISUAL_SEARCH_MAX,
                )
                by_id = {p["id"]: p for p in all_candidates}
                for pid in ids:
                    if pid in by_id:
                        result_map[pid] = by_id[pid]
                visual_checked = checked
            except SemanticSearchError as exc:
                semantic_warning = str(exc)
                if not result_map:
                    return jsonify({"error": semantic_warning}), 502

        photos = sort_photos(list(result_map.values()), sort)
        return jsonify(
            {
                "photos": [_photo_payload(p) for p in photos],
                "warning": semantic_warning,
                # How many pictures the model actually looked at. Without it the
                # visual mode looks like it scanned the library when it only
                # examined the first few dozen.
                "checked": visual_checked if mode == "visual" else None,
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
