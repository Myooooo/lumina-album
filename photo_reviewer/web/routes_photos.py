"""Gallery list, search, stats and one photo.

Registered by :func:`photo_reviewer.server.create_app`; the route bodies are
grouped by the part of the app they serve.
"""

from __future__ import annotations

from flask import jsonify, request, url_for

from .. import culling, stats_report, vectors, video

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
    hidden_folder_list,
)


def register(app, cfg, database):
    """Register these routes on ``app``."""

    @app.get("/api/photos")
    def api_photos():
        raw_folder = request.args.get("folder", "").strip()
        folder = None if (not raw_folder or raw_folder == "__all__") else raw_folder
        exclude_folders = hidden_folder_list(database) if folder is None else None
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
            exclude_folders=exclude_folders,
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

        if mode in ("keyword", "smart"):
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
        vector_scores = None
        if mode == "smart" and query:
            if vectors.has_clip_onnx_model(cfg):
                vec_candidates = database.list_photos(
                    folder=folder,
                    status=status,
                    min_score=min_score,
                    tag=tag,
                    favorite=favorite,
                    year=year,
                    exclude_deleted=exclude_deleted,
                    sort=sort,
                    limit=20000,
                )
                vres = vectors.vector_text_search(database, cfg, query, vec_candidates)
                semantic_warning = vres.get("warning")
                if vres.get("photos"):
                    vector_scores = vres.get("scores") or {}
                    for p in vres["photos"]:
                        result_map[p["id"]] = p
            else:
                reachable, why = probe_endpoint(cfg, timeout=SEARCH_PROBE_TIMEOUT)
                if not reachable:
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

        if mode == "visual" and query:
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
                "vector_scores": vector_scores,
                "checked": visual_checked if mode == "visual" else None,
            }
        )

    @app.get("/api/photo/<int:photo_id>/similar")
    def api_photo_similar(photo_id: int):
        """Image-to-image visual similarity search."""
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        folder = request.args.get("folder", "").strip() or photo.get("folder") or None
        try:
            limit = max(4, min(int(request.args.get("limit", 48)), 120))
        except (TypeError, ValueError):
            limit = 48
        try:
            min_sim = float(request.args.get("min_similarity", 0.68))
        except (TypeError, ValueError):
            min_sim = 0.68
        candidates = database.list_photos(
            folder=folder,
            exclude_deleted=True,
            sort="time_desc",
            limit=5000,
        )
        res = vectors.vector_image_search(
            database, cfg, photo, candidates, top_k=limit, min_similarity=min_sim
        )
        return jsonify(
            {
                "target": _photo_payload(photo),
                "model": res.get("model"),
                "scores": res.get("scores") or {},
                "photos": [_photo_payload(p) for p in res.get("photos", [])],
            }
        )

    @app.get("/api/vectors/status")
    def api_vectors_status():
        folder = request.args.get("folder", "").strip() or None
        return jsonify(
            {
                "engine": vectors.vector_engine_status(database, cfg, folder=folder),
                "job": vectors.VECTOR_INDEX_MANAGER.status(),
            }
        )

    @app.post("/api/vectors/index")
    def api_vectors_index():
        data = request.get_json(force=True, silent=True) or {}
        folder = (data.get("folder") or "").strip() or None
        force = bool(data.get("force"))
        job = vectors.VECTOR_INDEX_MANAGER.start(database, cfg, folder=folder, force=force)
        return jsonify(job)

    @app.post("/api/vectors/index/cancel")
    def api_vectors_index_cancel():
        vectors.VECTOR_INDEX_MANAGER.cancel()
        return jsonify(vectors.VECTOR_INDEX_MANAGER.status())

    @app.post("/api/vectors/models/download")
    def api_vectors_models_download():
        data = request.get_json(force=True, silent=True) or {}
        install_deps = bool(data.get("install_deps", True))
        job = vectors.VECTOR_MODEL_DOWNLOAD_MANAGER.start(cfg, install_deps=install_deps)
        return jsonify({"job": job})

    @app.get("/api/vectors/models/download/status")
    def api_vectors_models_download_status():
        return jsonify({"job": vectors.VECTOR_MODEL_DOWNLOAD_MANAGER.status()})

    @app.post("/api/vectors/models/download/cancel")
    def api_vectors_models_download_cancel():
        vectors.VECTOR_MODEL_DOWNLOAD_MANAGER.cancel()
        return jsonify({"job": vectors.VECTOR_MODEL_DOWNLOAD_MANAGER.status()})

    @app.get("/api/stats")
    def api_stats():
        raw_folder = request.args.get("folder", "").strip()
        folder = None if (not raw_folder or raw_folder == "__all__") else raw_folder
        exclude_folders = hidden_folder_list(database) if folder is None else None
        return jsonify(database.stats(folder, exclude_folders=exclude_folders))

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
                "faces": database.faces_for_photo(photo["id"]),
            }
        )

    @app.post("/api/cull/quality")
    def api_cull_quality():
        """Zero-VLM focus sharpness and exposure clipping check for culling."""
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"results": {}})
        results = {}
        for raw_id in ids[:24]:
            try:
                pid = int(raw_id)
            except (TypeError, ValueError):
                continue
            photo = database.get_photo(pid)
            if not photo:
                continue
            img_path = photo.get("proxy_path") or photo.get("thumb_path") or photo.get("path")
            metrics = culling.analyze_photo_quality(img_path)
            if metrics:
                results[str(pid)] = metrics
        return jsonify({"results": results})

    @app.post("/api/cull/export-originals")
    def api_cull_export_originals():
        """Copy or hardlink selected original photos (and RAW siblings) to a folder."""
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids") or []
        target_dir = (data.get("target_dir") or "").strip()
        mode = (data.get("mode") or "copy").strip().lower()
        include_raw = bool(data.get("include_raw", True))
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请先选择要导出的照片"}), 400
        if not target_dir:
            return jsonify({"error": "请填写导出目标目录"}), 400
        photos = []
        for raw_id in ids:
            try:
                pid = int(raw_id)
            except (TypeError, ValueError):
                continue
            p = database.get_photo(pid)
            if p:
                photos.append(p)
        if not photos:
            return jsonify({"error": "未找到选中的照片"}), 404
        try:
            report = culling.export_originals_to_dir(
                photos, target_dir=target_dir, mode=mode, include_raw=include_raw
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError as exc:
            return jsonify({"error": f"导出失败：{exc}"}), 500
        return jsonify(report)

    @app.get("/api/smart-albums")
    def api_list_smart_albums():
        return jsonify({"albums": database.list_smart_albums()})

    @app.post("/api/smart-albums")
    def api_create_smart_album():
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("name") or "").strip()
        rules = data.get("rules") if isinstance(data.get("rules"), dict) else {}
        try:
            album = database.create_smart_album(name, rules)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(album), 201

    @app.delete("/api/smart-albums/<int:album_id>")
    def api_delete_smart_album(album_id: int):
        deleted = database.delete_smart_album(album_id)
        if not deleted:
            return jsonify({"error": "智能影集不存在"}), 404
        return jsonify({"deleted": True, "id": album_id})

    @app.get("/api/stats/report")
    def api_stats_report():
        """Photography Year-in-Review & gear/focal statistics."""
        raw_folder = request.args.get("folder", "").strip()
        folder = None if (not raw_folder or raw_folder == "__all__") else raw_folder
        exclude_folders = hidden_folder_list(database) if folder is None else None
        year_raw = request.args.get("year", "").strip()
        year = None
        if year_raw:
            try:
                year = int(year_raw)
            except (TypeError, ValueError):
                return jsonify({"error": "year 参数不合法"}), 400
        rows = database.list_photos(
            folder=folder,
            exclude_deleted=True,
            exclude_folders=exclude_folders,
            sort="time_desc",
            limit=50000,
        )
        return jsonify(stats_report.build_shooting_report(rows, year=year))
