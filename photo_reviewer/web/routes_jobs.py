"""Scans, imports, camera cards and job status.

Registered by :func:`photo_reviewer.server.create_app`; the route bodies are
grouped by the part of the app they serve.
"""

from __future__ import annotations

import os

from flask import jsonify, request

from ..cards import (
    apply_pending,
    card_status,
    forget_card,
    pending_preview,
)

from ..importer import ImportError_

from ..paths import canonical_path

from ..scanner import (
    JOBS,
    JobConflict,
    start_rebuild_index,
    start_scan,
)

from ..sync import (
    build_plan,
    start_import,
)

from .helpers import (
    _as_bool,
    _cached_years,
    _job_payload,
    _normalize_folder,
)


def register(app, cfg, database):
    """Register these routes on ``app``."""

    @app.post("/api/rebuild-index")
    def api_rebuild_index():
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip()
        force = bool(data.get("force", False))
        skip_videos = bool(data.get("skip_indexed_videos", False))
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        try:
            folder = _normalize_folder(folder)
        except NotADirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            job = start_rebuild_index(
                folder, database, cfg, force=force, skip_indexed_videos=skip_videos
            )
        except JobConflict as exc:
            return jsonify(
                {"error": "该文件夹已有任务正在运行", "job_id": exc.job.id}
            ), 409
        return jsonify(
            {
                "job_id": job.id,
                "status": job.status,
                "folder": folder,
                "phase": job.phase,
            }
        )

    @app.post("/api/scan")
    def api_scan():
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip()
        force = bool(data.get("force", False))
        # The answer to "ffmpeg is missing - keep the videos I already read".
        skip_videos = bool(data.get("skip_indexed_videos", False))
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        try:
            folder = _normalize_folder(folder)
        except NotADirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        # The "one job per folder" rule lives in JobManager.create, so it cannot
        # be forgotten by a caller; this just turns the refusal into a 409.
        try:
            job = start_scan(
                folder, database, cfg, force=force, skip_indexed_videos=skip_videos
            )
        except JobConflict as exc:
            return jsonify(
                {"error": "该文件夹已有扫描任务正在运行", "job_id": exc.job.id}
            ), 409
        return jsonify({"job_id": job.id, "status": job.status, "folder": folder})

    @app.post("/api/import/plan")
    def api_import_plan():
        """Preview an SD-card import without copying anything."""
        data = request.get_json(force=True, silent=True) or {}
        try:
            plan = build_plan(data, cfg)
        except ImportError_ as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"plan": plan.summary()})

    @app.post("/api/import/run")
    def api_import_run():
        """Copy the new files from a card, then index what arrived."""
        data = request.get_json(force=True, silent=True) or {}
        try:
            job = start_import(data, database, cfg)
        except ImportError_ as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "job_id": job.id,
                "status": job.status,
                "phase": job.phase,
                "folder": job.folder,
                "total": job.total,
            }
        )

    @app.get("/api/card/status")
    def api_card_status():
        """Which camera albums exist, whether their card is here, and the queue.

        Cheap on purpose (``isdir``, ``os.access``, one ``os.stat`` per card), so
        the toolbar can poll it to keep the "SD card connected" badge honest.
        """
        folder = request.args.get("folder", "").strip() or None
        folder = canonical_path(folder) if folder else None
        cards = card_status(database, folder)
        current = None
        if folder:
            current = next((c for c in cards if c["album"] == folder), None)
        return jsonify({"cards": cards, "current": current})

    @app.post("/api/card/pending")
    def api_card_pending():
        """Preview the queued card work without touching the card."""
        data = request.get_json(force=True, silent=True) or {}
        source = data.get("source", "").strip() or None
        return jsonify({"preview": pending_preview(database, source)})

    @app.post("/api/card/sync")
    def api_card_sync():
        """Carry the queued deletions and restores out on the card."""
        data = request.get_json(force=True, silent=True) or {}
        source = canonical_path(data.get("source", "").strip())
        if not source:
            return jsonify({"error": "请提供 SD 卡路径"}), 400
        include_raw = _as_bool(data.get("include_raw", True), default=True)
        result = apply_pending(database, source, cfg.trash_dir_name, include_raw)
        return jsonify(result)

    @app.post("/api/card/forget")
    def api_card_forget():
        """Stop syncing a folder with its card. Never deletes any file."""
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip()
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        source = data.get("source", "").strip() or None
        result = forget_card(database, canonical_path(folder), source)
        return jsonify(result)

    @app.get("/api/scan/jobs")
    def api_scan_jobs():
        folder = request.args.get("folder", "").strip() or None
        jobs = []
        for job in JOBS.list():
            if folder and job.folder != folder:
                continue
            jobs.append(_job_payload(job))
        jobs.sort(key=lambda item: (item["status"] != "running", item["job_id"]))
        return jsonify({"jobs": jobs})

    @app.get("/api/scan/status/<job_id>")
    def api_scan_status(job_id: str):
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify(_job_payload(job))

    @app.post("/api/scan/cancel/<job_id>")
    def api_scan_cancel(job_id: str):
        ok = JOBS.cancel(job_id)
        return jsonify({"cancelled": ok})

    @app.get("/api/tags")
    def api_tags():
        folder = request.args.get("folder", "").strip() or None
        return jsonify({"tags": database.all_tags(folder)})

    @app.get("/api/years")
    def api_years():
        folder = request.args.get("folder", "").strip() or None
        return jsonify({"years": _cached_years(database, folder)})

    @app.get("/api/folders")
    def api_folders():
        return jsonify({"folders": database.all_folders()})
