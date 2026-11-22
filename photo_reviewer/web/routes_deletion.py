"""Trash, permanent deletion, edits and batch analysis.

Registered by :func:`photo_reviewer.server.create_app`; the route bodies are
grouped by the part of the app they serve.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import logging

from flask import jsonify, request

from ..api_client import (
    DIMENSION_KEYS,
    probe_endpoint,
)

from ..cache import (
    cache_dir_for_folder,
    cleanup_folder_cache,
)

from ..cards import (
    CardError,
    forget_card,
    purge_for_photo,
    target_for,
)

from ..paths import canonical_path

from ..scanner import (
    JOBS,
    reanalyze_photo,
    start_analysis_batch,
)

from .helpers import (
    _as_bool,
    _clean_dimensions,
    _cleanup_cache,
    _empty_trash,
    _empty_trash_worker,
    _move_to_trash,
    _normalize_folder,
    _photo_payload,
    _remove_file,
    _remove_photo_cache,
    _restore_from_trash,
    _sync_delete_to_card,
    _sync_restore_to_card,
    _trash_only,
    _trashed_live_candidate,
    _trashed_raw_candidate,
    set_folder_hidden,
)

from datetime import date

logger = logging.getLogger(__name__)


def register(app, cfg, database):
    """Register these routes on ``app``."""

    @app.post("/api/delete")
    def api_delete():
        """Move photos to the trash, and optionally off the camera card too.

        ``sync_card`` is the answer to the "also delete from the SD card"
        checkbox, which the UI only offers while that card is connected. When the
        card is away the answer is false by construction and the deletion is
        queued for the next time the card shows up.
        """
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请选择要删除的照片"}), 400
        sync_card = _as_bool(data.get("sync_card", False))
        moved = []
        errors = []
        touched_folders = set()
        card_totals = {"synced": 0, "queued": 0, "skipped": 0, "errors": []}
        any_card = False
        for pid in ids:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                errors.append({"id": pid, "error": "无效 ID"})
                continue
            photo = database.get_photo(pid)
            if not photo:
                errors.append({"id": pid, "error": "照片不存在"})
                continue
            if photo.get("status") == "deleted":
                errors.append({"id": pid, "error": "照片已在回收站"})
                continue
            # The card side is decided before the local move, because moving the
            # raw sibling clears raw_path and loses the half of the pair that
            # also has to disappear from the card.
            raw_path = photo.get("raw_path")
            try:
                card_result = _sync_delete_to_card(
                    database, cfg, photo, raw_path, sync_card
                )
            except Exception:
                logger.warning("card sync failed for photo %s", pid, exc_info=True)
                card_result = None
            try:
                new_path, trashed_raw = _move_to_trash(photo, cfg)
                database.mark_deleted([photo["id"]], {photo["id"]: new_path})
                if photo.get("raw_path"):
                    # The raw is no longer beside the image, but the row keeps
                    # pointing at where it went: the pair is still a pair, and
                    # "彻底移除" has to be able to offer deleting both halves.
                    database.update_raw_path(photo["id"], trashed_raw)
                moved.append({"id": photo["id"], "path": new_path})
                touched_folders.add(
                    photo.get("folder") or os.path.dirname(photo["path"])
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": pid, "error": str(exc)})
                continue
            if card_result:
                any_card = True
                for key in ("synced", "queued", "skipped"):
                    card_totals[key] += card_result[key]
                card_totals["errors"].extend(card_result["errors"])
                card_totals["source"] = card_result["source"]
        # No folder-wide cache sweep here: moving a photo to the trash keeps its
        # row and every cached path it references, so a sweep would scan the
        # whole cache directory to find nothing. It only earns its cost when a
        # row is actually deleted, which is what "彻底移除" does.
        payload = {"moved": moved, "errors": errors}
        if any_card:
            payload["card"] = card_totals
        return jsonify(payload)

    @app.post("/api/restore")
    def api_restore():
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请选择要恢复的照片"}), 400
        sync_card = _as_bool(data.get("sync_card", False))
        restored = []
        errors = []
        touched_folders = set()
        card_totals = {"restored": 0, "queued": 0, "errors": []}
        any_card = False
        for pid in ids:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                errors.append({"id": pid, "error": "无效 ID"})
                continue
            photo = database.get_photo(pid)
            if not photo:
                errors.append({"id": pid, "error": "照片不存在"})
                continue
            if photo.get("status") != "deleted":
                errors.append({"id": pid, "error": "照片不在回收站"})
                continue
            try:
                new_original, restored_raw = _restore_from_trash(photo, config=cfg)
                database.restore_deleted([photo["id"]], {photo["id"]: new_original})
                if restored_raw:
                    database.update_raw_path(photo["id"], restored_raw)
                restored.append({"id": photo["id"], "path": new_original})
                touched_folders.add(
                    photo.get("folder")
                    or os.path.dirname(photo.get("original_path") or photo["path"])
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": pid, "error": str(exc)})
                continue
            try:
                card_result = _sync_restore_to_card(
                    database, photo, restored_raw, sync_card
                )
            except Exception:
                logger.warning("card restore failed for photo %s", pid, exc_info=True)
                card_result = None
            if card_result:
                any_card = True
                card_totals["restored"] += card_result["restored"]
                card_totals["queued"] += card_result["queued"]
                card_totals["errors"].extend(card_result["errors"])
                card_totals["source"] = card_result["source"]
        # Same reasoning as delete: restoring reuses the cache the row still
        # points at, so there is nothing to sweep.
        payload = {"restored": restored, "errors": errors}
        if any_card:
            payload["card"] = card_totals
        return jsonify(payload)

    @app.post("/api/delete/permanent")
    def api_delete_permanent():
        """Permanently remove photos that are already in the trash.

        ``delete_raw`` (default on) also removes the raw sibling that was moved
        to the trash with the photo.
        """
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请选择要永久删除的照片"}), 400
        delete_raw = _as_bool(data.get("delete_raw", True), default=True)
        purged = []
        errors = []
        raw_removed = 0
        live_removed = 0
        card_removed = 0
        card_deferred = 0
        touched_folders = set()
        for pid in ids:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                errors.append({"id": pid, "error": "无效 ID"})
                continue
            photo = database.get_photo(pid)
            if not photo:
                errors.append({"id": pid, "error": "照片不存在"})
                continue
            if photo.get("status") != "deleted":
                errors.append({"id": pid, "error": "只有已删除的照片可以永久删除"})
                continue
            path = photo.get("path") or photo.get("original_path")
            file_removed = False
            if path and os.path.exists(path):
                if not _trash_only(path, cfg):
                    errors.append(
                        {
                            "id": pid,
                            "error": "该记录指向的不是回收站文件，已跳过以免误删原图",
                        }
                    )
                    continue
                try:
                    _remove_file(path)
                    file_removed = True
                except OSError as exc:
                    errors.append({"id": pid, "error": f"删除文件失败: {exc}"})
                    continue
            # The raw sibling sits in the same trash day folder; purging the
            # photo must not leave it behind.
            if delete_raw:
                sibling = _trashed_raw_candidate(photo)
                if sibling and _trash_only(sibling, cfg):
                    try:
                        _remove_file(sibling)
                        raw_removed += 1
                    except OSError:
                        logger.debug("raw sibling already gone: %s", sibling)
            # A Live Photo's movie went to the trash with the still, so purging
            # the photo has to take it too - otherwise "彻底移除" leaves a movie
            # in the trash with no card pointing at it.
            live_sibling = _trashed_live_candidate(photo, cfg)
            if live_sibling and _trash_only(live_sibling, cfg):
                try:
                    _remove_file(live_sibling)
                    live_removed += 1
                except OSError:
                    logger.debug("live photo movie already gone: %s", live_sibling)
            # A file that was moved into the card's trash earlier is erased now,
            # so permanently removing a photo does not leave a copy behind on the
            # card. A card that is away defers this to its next connection.
            try:
                card_result = purge_for_photo(
                    database, target_for(database, photo.get("folder") or ""), pid
                )
                card_removed += card_result["purged"]
                card_deferred += card_result["deferred"]
            except (CardError, OSError):
                logger.warning("card purge failed for photo %s", pid, exc_info=True)
            touched_folders.add(
                photo.get("folder")
                or os.path.dirname(photo.get("original_path") or photo["path"])
            )
            _remove_photo_cache(photo, cfg)
            database.delete_rows([pid])
            purged.append({"id": pid, "file_removed": file_removed})
        # One photo's own files were just deleted by name; sweeping the whole
        # cache directory afterwards would only re-stat every file in it to find
        # nothing. The sweep earns its cost when several rows went away at once,
        # because then there may be orphans this request did not name.
        if len(purged) > 1:
            for folder in touched_folders:
                try:
                    cleanup_folder_cache(folder, database, cfg.cache_dir_name)
                except (OSError, sqlite3.Error):
                    logger.warning("cache cleanup failed for %s", folder, exc_info=True)
        return jsonify(
            {
                "purged": purged,
                "raw_removed": raw_removed,
                "live_removed": live_removed,
                "card_removed": card_removed,
                "card_deferred": card_deferred,
                "errors": errors,
            }
        )

    @app.post("/api/folder/remove")
    def api_folder_remove():
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip()
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        folder = canonical_path(folder)
        for existing in JOBS.list():
            if existing.folder == folder and existing.status == "running":
                return jsonify({"error": "该文件夹正在整理中，请稍后再试"}), 409
        removed = database.remove_folder(folder)
        # The album is gone from the library, so a "hidden" mark on it would
        # only leave a dead row in the hidden list; drop it with the index.
        try:
            set_folder_hidden(database, folder, False)
        except ValueError:
            pass
        # The folder is no longer indexed, so it is no longer a camera album
        # either; forgetting the pairing drops queued card work with it. No file
        # on the card is touched by this.
        links = forget_card(database, folder)
        cache_dir = cache_dir_for_folder(folder, cfg.cache_dir_name)
        cache_removed = False
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_removed = not cache_dir.exists()
        return jsonify(
            {
                "removed": removed,
                "cache_removed": cache_removed,
                "card_links_removed": links["pairings_removed"],
            }
        )

    @app.post("/api/photo/<int:photo_id>/edit")
    def api_photo_edit(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        data = request.get_json(force=True, silent=True) or {}
        try:
            score = float(data.get("score", photo.get("score") or 0.0))
            score = max(0.0, min(10.0, score))
        except (TypeError, ValueError):
            return jsonify({"error": "总分不合法"}), 400
        dimensions = _clean_dimensions(photo.get("dimensions"))
        raw_dimensions = data.get("dimensions") or {}
        if isinstance(raw_dimensions, dict):
            for key in DIMENSION_KEYS:
                if key in raw_dimensions:
                    try:
                        dimensions[key] = max(
                            0.0, min(10.0, float(raw_dimensions[key]))
                        )
                    except (TypeError, ValueError):
                        return jsonify({"error": f"{key} 评分不合法"}), 400
        tags = data.get("tags", photo.get("tags") or [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
        if not isinstance(tags, list):
            return jsonify({"error": "标签不合法"}), 400
        tags = [str(t).strip() for t in tags if str(t).strip()][:20]
        reason = str(data.get("reason", photo.get("reason") or "")).strip()
        title = str(data.get("title", photo.get("title") or "")).strip()[:40]
        location_value = data.get("location", photo.get("location") or "")
        location = str(location_value).strip() if location_value else None
        database.update_metadata(
            photo_id, score, dimensions, tags, reason, title, location
        )
        updated = database.get_photo(photo_id)
        return jsonify(_photo_payload(updated))

    @app.post("/api/trash/empty")
    def api_trash_empty():
        """Permanently delete everything in the trash.

        ``delete_raw`` (default on) also removes the raw sibling that was moved
        to the trash together with each photo, so emptying the bin does not
        leave orphaned raw files behind.

        ``background`` (used by the UI) runs the sweep as a tracked job and
        returns its id, so the toolbar can show a progress bar. The synchronous
        form is kept for scripts and tests, which want the counts directly.
        """
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip() or None
        delete_raw = _as_bool(data.get("delete_raw", True), default=True)
        background = _as_bool(data.get("background", False), default=False)
        if folder:
            folder = canonical_path(folder)

        if background:
            # Count before starting the thread: the progress bar needs a total
            # from the very first tick, and a caller that polls immediately
            # should not see "0/0" and conclude there is nothing to do.
            pending = database.list_photos(
                folder=folder,
                status="deleted",
                exclude_deleted=False,
                limit=100000,
            )
            # Emptying the bin writes files, not index rows, so it can run
            # alongside a scan of the same folder.
            job = JOBS.create(folder or "", False, exclusive=False)
            job.phase = "trash"
            job.total = len(pending)
            thread = threading.Thread(
                target=_empty_trash_worker,
                args=(job.id, database, cfg, folder, delete_raw),
                daemon=True,
            )
            thread.start()
            return jsonify({"job_id": job.id, "status": job.status, "total": job.total})

        return jsonify(_empty_trash(database, cfg, folder, delete_raw))

    @app.post("/api/cache/clean")
    def api_cache_clean():
        data = request.get_json(force=True, silent=True) or {}
        folder = request.args.get("folder", "").strip() or data.get("folder", "")
        if folder:
            folder = canonical_path(folder)
            result = cleanup_folder_cache(folder, database, cfg.cache_dir_name)
        else:
            result = _cleanup_cache(database, cfg.cache_dir_name)
        return jsonify(result)

    @app.post("/api/favorite")
    def api_favorite():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id")
        if pid is None:
            return jsonify({"error": "缺少照片 id"}), 400
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return jsonify({"error": "照片 id 不合法"}), 400
        raw_favorite = data.get("favorite", True)
        favorite = (
            raw_favorite
            if isinstance(raw_favorite, bool)
            else str(raw_favorite).strip().lower() in ("1", "true", "yes", "on")
        )
        photo = database.get_photo(pid)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        database.set_favorite(pid, favorite)
        photo["favorite"] = favorite
        return jsonify(_photo_payload(photo))

    @app.post("/api/reanalyze")
    def api_reanalyze():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id")
        if pid is None:
            return jsonify({"error": "缺少照片 id"}), 400
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return jsonify({"error": "照片 id 不合法"}), 400
        try:
            photo = reanalyze_photo(pid, database, cfg)
            return jsonify(_photo_payload(photo))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/analyze/batch")
    def api_analyze_batch():
        """Analyse a set of photos as one background job.

        This is what the "重新解读" action on a multi-photo selection calls. It
        used to be a browser-side loop issuing one request per photo behind a
        blocking overlay, which meant no cancelling, no progress beyond a
        counter, and starting over after a refresh. Running it as a job gives
        the toolbar bar, the remaining-time estimate, cancel, and resumption
        after a page reload - all of which already existed for scans.

        ``ids`` may be omitted in favour of ``folder`` + ``status="pending"``,
        which queues everything in an album that has no analysis yet.
        """
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids")
        folder = data.get("folder", "").strip()
        status = data.get("status", "").strip() or None

        if ids is None:
            if not folder:
                return jsonify({"error": "请提供照片 id 或文件夹"}), 400
            try:
                folder = _normalize_folder(folder)
            except NotADirectoryError as exc:
                return jsonify({"error": str(exc)}), 400
            rows = database.list_photos(
                folder=folder,
                status=status,
                limit=100000,
            )
            photo_ids = [row["id"] for row in rows]
        else:
            if not isinstance(ids, list) or not ids:
                return jsonify({"error": "请至少选择一张照片"}), 400
            try:
                photo_ids = [int(value) for value in ids]
            except (TypeError, ValueError):
                return jsonify({"error": "照片 id 不合法"}), 400

        usable = []
        for pid in photo_ids:
            row = database.get_photo(pid)
            if row and row.get("status") != "deleted":
                usable.append(pid)
        if not usable:
            return jsonify({"error": "没有可以重新解读的照片"}), 400

        # One cheap request now beats failing every photo in turn: with the
        # endpoint down each attempt burns its full retry budget (~15 s).
        reachable, why = probe_endpoint(cfg)
        if not reachable:
            return jsonify({"error": why}), 503

        job = start_analysis_batch(folder, usable, database, cfg)
        return jsonify(
            {
                "job_id": job.id,
                "status": job.status,
                "phase": job.phase,
                "total": job.total,
            }
        )
