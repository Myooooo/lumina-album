"""People & Pets (人物与宠物) recognition, clustering, labeling and merging routes.

Registered by :func:`photo_reviewer.server.create_app`.
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import jsonify, request, send_file

from ..faces import (
    FACE_MODELS,
    MODEL_DOWNLOADER,
    SCAN_COORDINATOR,
    crop_entity_thumbnail,
    engine_status,
    face_thumbs_dir,
    migrate_single_face_thumbnail,
    match_and_cluster_library,
    recompute_person_centroid,
)
from .helpers import _photo_payload


def register(app, cfg, database):
    """Register People & Pets API routes on ``app``."""

    @app.get("/api/people/status")
    def api_people_status():
        st = engine_status(cfg)
        st["job"] = SCAN_COORDINATOR.status()
        st["download_job"] = MODEL_DOWNLOADER.status()
        return jsonify(st)

    @app.post("/api/people/scan")
    def api_people_scan():
        data = request.get_json(force=True, silent=True) or {}
        folder = str(data.get("folder") or "").strip() or None
        force = bool(data.get("force", False))
        st = engine_status(cfg)
        if not st["ready"]:
            return (
                jsonify(
                    {
                        "error": "本地人物/宠物模型尚未就绪，请先在「设置 → 人物」中检查依赖或下载本地模型。",
                        "status": st,
                    }
                ),
                400,
            )
        job = SCAN_COORDINATOR.start(database, cfg, folder=folder, force=force)
        return jsonify({"job": job})

    @app.post("/api/people/scan/cancel")
    def api_people_scan_cancel():
        cancelled = SCAN_COORDINATOR.cancel()
        return jsonify({"cancelled": cancelled, "job": SCAN_COORDINATOR.status()})

    @app.post("/api/people/recluster")
    def api_people_recluster():
        summary = match_and_cluster_library(database, cfg)
        return jsonify({"ok": True, **summary})

    @app.post("/api/people/models/download")
    def api_people_download_models():
        data = request.get_json(force=True, silent=True) or {}
        model_name = str(data.get("model") or cfg.face_model or "buffalo_l").strip()
        if model_name not in FACE_MODELS:
            return jsonify({"error": "不支持的模型选项"}), 400
        raw_pets = data.get("include_pets", data.get("detect_pets", cfg.face_detect_pets))
        include_pets = bool(raw_pets)
        try:
            job = MODEL_DOWNLOADER.start(cfg, model_name=model_name, include_pets=include_pets)
            return jsonify({"ok": True, "job": job, "status": engine_status(cfg)})
        except Exception as exc:
            return jsonify({"error": f"启动模型下载失败：{exc}"}), 500

    @app.get("/api/people/models/download/status")
    def api_people_download_status():
        return jsonify({"job": MODEL_DOWNLOADER.status(), "status": engine_status(cfg)})

    @app.post("/api/people/models/download/cancel")
    def api_people_download_cancel():
        cancelled = MODEL_DOWNLOADER.cancel()
        return jsonify({"cancelled": cancelled, "job": MODEL_DOWNLOADER.status()})

    @app.get("/api/people")
    def api_list_people():
        folder = request.args.get("folder", "").strip() or None
        kind = request.args.get("kind", "").strip().lower() or None
        if kind not in (None, "person", "cat", "dog", "pet"):
            kind = None
        include_hidden = request.args.get("include_hidden", "0") in ("1", "true", "yes")
        include_singles = request.args.get("include_singles", "0") in ("1", "true", "yes")
        min_unnamed = 1 if include_singles else max(1, int(getattr(cfg, "face_min_cluster_size", 5)))
        people = database.list_people(
            folder=folder,
            include_hidden=include_hidden,
            kind=kind,
            min_unnamed_count=min_unnamed,
        )
        return jsonify(
            {
                "people": people,
                "total": len(people),
                "status": engine_status(cfg),
                "job": SCAN_COORDINATOR.status(),
            }
        )

    @app.get("/api/people/<int:person_id>")
    def api_get_person(person_id: int):
        person = database.get_person(person_id)
        if not person:
            return jsonify({"error": "人物不存在"}), 404
        folder = request.args.get("folder", "").strip() or None
        confirmed_photos = database.person_photos(person_id, folder=folder, pending=False)
        pending_photos = database.person_photos(person_id, folder=folder, pending=True)
        pending_faces = database.person_pending_faces(person_id, folder=folder)
        cover_faces = database.faces_for_person(person_id, include_pending=False)[:24]
        return jsonify(
            {
                "person": {
                    "id": person["id"],
                    "name": person.get("name") or "",
                    "kind": person.get("kind") or "person",
                    "cover_face_id": person.get("cover_face_id"),
                    "is_hidden": bool(person.get("is_hidden")),
                    "is_favorite": bool(person.get("is_favorite")),
                    "count": len(confirmed_photos),
                },
                "photos": [_photo_payload(p) for p in confirmed_photos],
                "pending_photos": [_photo_payload(p) for p in pending_photos],
                "pending_faces": pending_faces,
                "cover_candidates": [
                    {
                        "face_id": f["id"],
                        "photo_id": f["photo_id"],
                        "det_score": f.get("det_score") or 0.0,
                        "thumb_url": f"/api/faces/{f['id']}/thumb",
                    }
                    for f in cover_faces
                ],
            }
        )

    @app.patch("/api/people/<int:person_id>")
    def api_update_person(person_id: int):
        person = database.get_person(person_id)
        if not person:
            return jsonify({"error": "人物不存在"}), 404
        data = request.get_json(force=True, silent=True) or {}

        if "name" in data:
            new_name = str(data.get("name") or "").strip()[:40]
            merge_if_exists = bool(data.get("merge_if_exists", False))
            if new_name:
                existing = database.find_person_by_name(new_name, exclude_id=person_id)
                if existing:
                    if not merge_if_exists:
                        return (
                            jsonify(
                                {
                                    "conflict": True,
                                    "existing": {
                                        "id": existing["id"],
                                        "name": existing["name"],
                                        "kind": existing.get("kind") or "person",
                                        "cover_face_id": existing.get("cover_face_id"),
                                    },
                                    "message": f"已存在名为「{new_name}」的人物，是否合并？",
                                }
                            ),
                            409,
                        )
                    database.merge_people([person_id], existing["id"])
                    recompute_person_centroid(database, existing["id"])
                    match_summary = match_and_cluster_library(database, cfg)
                    return jsonify(
                        {
                            "merged_into": existing["id"],
                            "person": database.get_person(existing["id"]),
                            "match_summary": match_summary,
                        }
                    )
            database.rename_person(person_id, new_name)
            recompute_person_centroid(database, person_id)
            # Naming a person immediately helps pull matching unassigned faces.
            if new_name:
                match_and_cluster_library(database, cfg)

        if any(k in data for k in ("is_hidden", "is_favorite", "cover_face_id")):
            database.set_person_flags(
                person_id,
                is_hidden=bool(data["is_hidden"]) if "is_hidden" in data else None,
                is_favorite=bool(data["is_favorite"]) if "is_favorite" in data else None,
                cover_face_id=int(data["cover_face_id"])
                if data.get("cover_face_id") is not None
                else None,
            )

        updated = database.get_person(person_id)
        return jsonify({"ok": True, "person": updated})

    @app.post("/api/people/merge")
    def api_merge_people():
        data = request.get_json(force=True, silent=True) or {}
        raw_sources = data.get("source_ids") or []
        target_id = data.get("target_id")
        try:
            target_id = int(target_id)
            source_ids = [int(x) for x in raw_sources if int(x) != target_id]
        except (TypeError, ValueError):
            return jsonify({"error": "合并参数不合法"}), 400
        if not source_ids or not database.get_person(target_id):
            return jsonify({"error": "请选择至少两个有效人物进行合并"}), 400
        database.merge_people(source_ids, target_id)
        recompute_person_centroid(database, target_id)
        summary = match_and_cluster_library(database, cfg)
        return jsonify(
            {
                "ok": True,
                "person": database.get_person(target_id),
                "match_summary": summary,
            }
        )

    @app.patch("/api/faces/<int:face_id>")
    def api_update_face(face_id: int):
        face = database.get_face(face_id)
        if not face:
            return jsonify({"error": "人脸记录不存在"}), 404
        data = request.get_json(force=True, silent=True) or {}
        action = str(data.get("action") or "").strip().lower()
        old_pid = face.get("person_id")

        if action == "confirm":
            if old_pid is None:
                return jsonify({"error": "该人脸尚未关联人物"}), 400
            database.set_face_assignment(face_id, old_pid, status="manual")
            recompute_person_centroid(database, old_pid)
            return jsonify({"ok": True, "face": database.get_face_public(face_id)})

        if action == "reject":
            # Reject a pending suggestion: return to unassigned or ignore.
            database.set_face_assignment(face_id, None, status="ignored")
            if old_pid:
                recompute_person_centroid(database, old_pid)
            return jsonify({"ok": True, "face": database.get_face_public(face_id)})

        if action == "assign":
            new_name = str(data.get("name") or "").strip()[:40]
            target_pid = data.get("person_id")
            if target_pid is not None:
                try:
                    target_pid = int(target_pid)
                except (TypeError, ValueError):
                    return jsonify({"error": "person_id 不合法"}), 400
            elif new_name:
                existing = database.find_person_by_name(new_name)
                if existing:
                    target_pid = existing["id"]
                else:
                    target_pid = database.create_person(
                        name=new_name,
                        kind=face.get("entity_kind") or "person",
                        cover_face_id=face_id,
                        centroid=face.get("embedding"),
                    )
            else:
                return jsonify({"error": "请提供目标人物或新姓名"}), 400

            database.set_face_assignment(face_id, target_pid, status="manual")
            if old_pid and old_pid != target_pid:
                recompute_person_centroid(database, old_pid)
            recompute_person_centroid(database, target_pid)
            match_and_cluster_library(database, cfg)
            return jsonify({"ok": True, "face": database.get_face_public(face_id)})

        return jsonify({"error": "不支持的操作类型"}), 400

    @app.get("/api/faces/<int:face_id>/thumb")
    def api_face_thumb(face_id: int):
        face = database.get_face(face_id)
        if not face:
            return jsonify({"error": "人脸不存在"}), 404
        thumb_path = face.get("thumb_path")
        if thumb_path and os.path.isfile(thumb_path):
            try:
                thumb_path = migrate_single_face_thumbnail(database, cfg, face) or thumb_path
            except Exception:
                pass
        if thumb_path and os.path.isfile(thumb_path):
            return send_file(thumb_path, mimetype="image/jpeg")
        photo = database.get_photo(face["photo_id"])
        if not photo:
            return jsonify({"error": "原图不存在"}), 404
        src = photo.get("proxy_path") or photo.get("path")
        if src and os.path.isfile(src):
            folder = photo.get("folder") or os.path.dirname(photo.get("path") or "")
            out = face_thumbs_dir(cfg, folder) / f"face_{face_id}.jpg"
            saved = crop_entity_thumbnail(
                src,
                (face["bbox_x"], face["bbox_y"], face["bbox_w"], face["bbox_h"]),
                out,
            )
            if saved and os.path.isfile(saved):
                database.update_face_thumb(face_id, saved)
                return send_file(saved, mimetype="image/jpeg")
        return jsonify({"error": "无法生成人物缩略图"}), 404
