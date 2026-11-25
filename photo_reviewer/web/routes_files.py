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
    drop_folder_from_ui_state,
    hidden_folder_list,
    set_folder_hidden,
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
        # A hidden album is only reachable from the settings panel, so the
        # browser must not offer it as a path either.
        hidden_albums = {
            os.path.normcase(canonical_path(item))
            for item in hidden_folder_list(database)
        }
        dirs = []
        for name in entries:
            if name in hidden:
                continue
            full = os.path.join(path, name)
            if os.path.normcase(full) in hidden_albums:
                continue
            if os.path.isdir(full) and not os.path.islink(full):
                dirs.append({"name": name, "path": full})
        parent = os.path.dirname(path)
        return jsonify({"path": path, "parent": parent, "directories": dirs})

    @app.get("/api/folders/hidden")
    def api_hidden_folders():
        """The albums hidden from the main path selectors."""
        return jsonify({"folders": hidden_folder_list(database)})

    @app.post("/api/folders/hide")
    def api_hide_folder():
        """Hide one album from the main path selectors.

        Nothing on disk and no index row is touched: the album keeps its photos,
        scores and cache, it is just not offered as a path any more. It stays
        reachable from the settings panel's hidden list.
        """
        data = request.get_json(force=True, silent=True) or {}
        folder = str(data.get("folder") or "").strip()
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        try:
            folders = set_folder_hidden(database, folder, True)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        resolved = canonical_path(folder)
        # A refresh must land on the last visible album, and the history
        # dropdown must not offer this one: drop it from the remembered state.
        drop_folder_from_ui_state(database, resolved)
        return jsonify({"folders": folders, "folder": resolved})

    @app.post("/api/folders/unhide")
    def api_unhide_folder():
        """Bring one album back to the main path selectors."""
        data = request.get_json(force=True, silent=True) or {}
        folder = str(data.get("folder") or "").strip()
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        try:
            folders = set_folder_hidden(database, folder, False)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"folders": folders})

    def _allowed_folder_roots() -> list[str]:
        from ..faces import models_root

        roots: list[str] = []
        for item in database.all_folders():
            if item:
                roots.append(canonical_path(item))
        for item in hidden_folder_list(database):
            if item:
                roots.append(canonical_path(item))
        for card in database.list_card_albums():
            for field in ("folder", "source"):
                val = card.get(field)
                if val:
                    roots.append(canonical_path(val))
        ui_state = database.get_ui_state()
        for field in ("folder", "import_source"):
            val = ui_state.get(field)
            if isinstance(val, str) and val.strip():
                roots.append(canonical_path(val))
        history = ui_state.get("folder_history")
        if isinstance(history, list):
            for val in history:
                if isinstance(val, str) and val.strip():
                    roots.append(canonical_path(val))
        roots.append(canonical_path(str(models_root(cfg))))
        roots.append(canonical_path(str(Path(cfg.data_dir).resolve())))
        return roots

    def _is_allowed_folder(path: str) -> bool:
        norm_target = os.path.normcase(canonical_path(path))
        for root in _allowed_folder_roots():
            norm_root = os.path.normcase(root)
            try:
                if os.path.commonpath([norm_target, norm_root]) == norm_root:
                    return True
            except ValueError:
                continue
        return False

    @app.post("/api/open-folder")
    def api_open_folder():
        data = request.get_json(force=True, silent=True) or {}
        path = data.get("path", "").strip()
        if not path:
            return jsonify({"error": "请提供文件夹路径"}), 400
        path = canonical_path(path)
        if not _is_allowed_folder(path):
            return jsonify({"error": "不允许打开未关联的系统目录"}), 403
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
