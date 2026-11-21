"""Filesystem browsing and opening files.

Registered by :func:`photo_reviewer.server.create_app`; the route bodies are
grouped by the part of the app they serve.
"""

from __future__ import annotations

import os

from pathlib import Path
import logging

from flask import jsonify, request

from ..paths import (
    canonical_path,
    same_path,
)

from .helpers import (
    _open_with_system,
)

logger = logging.getLogger(__name__)


def register(app, cfg, database):
    """Register these routes on ``app``."""

    @app.get("/api/dirs")
    def api_dirs():
        """List the sub-directories of one path, for the folder browser.

        The app's own directories are filtered out: ``.photo-review-cache`` and
        ``.photo-trash`` are hidden bookkeeping, and offering them as photo
        folders invites a user to index or delete their own cache by hand.
        """
        path = request.args.get("path", "").strip()
        if not path:
            path = str(Path.home())
        path = canonical_path(path)
        if not os.path.isdir(path):
            return jsonify({"error": "目录不存在"}), 404
        try:
            entries = sorted(os.listdir(path), key=str.lower)
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
        hidden = {cfg.cache_dir_name, cfg.trash_dir_name}
        dirs = []
        for name in entries:
            if name in hidden:
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full) and not os.path.islink(full):
                dirs.append({"name": name, "path": full})
        parent = os.path.dirname(path)
        return jsonify({"path": path, "parent": parent, "directories": dirs})

    @app.post("/api/open-folder")
    def api_open_folder():
        data = request.get_json(force=True, silent=True) or {}
        path = data.get("path", "").strip()
        if not path:
            return jsonify({"error": "请提供文件夹路径"}), 400
        path = canonical_path(path)
        if not os.path.isdir(path):
            return jsonify({"error": "文件夹不存在"}), 404
        try:
            _open_with_system(path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True})

    @app.post("/api/open-file")
    def api_open_file():
        """Hand one indexed photo (or its raw sibling) to the OS default app.

        The path is never trusted on its own: it must match either the stored
        ``path`` or the stored ``raw_path`` of an existing photo row, so this
        cannot be used to launch arbitrary files through the browser.
        """
        data = request.get_json(force=True, silent=True) or {}
        try:
            photo_id = int(data.get("photo_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "缺少照片 id"}), 400
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404

        candidates = [photo.get("path"), photo.get("raw_path"), photo.get("original_path")]
        requested = str(data.get("path") or "").strip()
        allowed = [c for c in candidates if c]
        if requested:
            resolved = canonical_path(requested)
            if not any(same_path(resolved, c) for c in allowed):
                return jsonify({"error": "该路径不属于这张照片"}), 403
        else:
            resolved = canonical_path(allowed[0]) if allowed else ""
        if not resolved or not os.path.isfile(resolved):
            return jsonify({"error": "文件不存在"}), 404
        try:
            _open_with_system(resolved)
        except Exception as exc:
            logger.warning("could not open %s", resolved, exc_info=True)
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True, "path": resolved})
