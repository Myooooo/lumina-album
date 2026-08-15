"""Flask web application and REST API for the local photo review system."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_file

from .api_client import SYSTEM_PROMPT, SemanticSearchError, semantic_search
from .cache import (
    cache_dir_for_folder,
    cleanup_folder_cache,
    ensure_cache_dirs,
    load_scan_results_from_cache,
)
from .config import PERSISTED_FIELDS, Config
from .db import Database, sort_photos
from .exif import extract_exif
from .geocode import reverse_geocode
from .scanner import JOBS, reanalyze_photo, start_rebuild_index, start_scan
from .thumbnailer import make_proxy

logger = logging.getLogger(__name__)


def _photo_payload(photo: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": photo["id"],
        "path": photo["path"],
        "original_path": photo.get("original_path"),
        "folder": photo["folder"],
        "filename": photo["filename"],
        "size": photo["size"],
        "width": photo["width"],
        "height": photo["height"],
        "thumb_path": photo.get("thumb_path"),
        "proxy_path": photo.get("proxy_path"),
        "dimensions": photo.get("dimensions") or {},
        "location": photo.get("location"),
        "exif": photo.get("exif") or {},
        "phash": photo.get("phash"),
        "score": photo.get("score"),
        "recommendation": photo.get("recommendation"),
        "tags": photo.get("tags") or [],
        "reason": photo.get("reason"),
        "title": photo.get("title") or "",
        "model": photo.get("model"),
        "analyzed_at": photo.get("analyzed_at"),
        "status": photo.get("status"),
        "favorite": bool(photo.get("favorite")),
        "error": photo.get("error"),
    }


def _normalize_folder(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isdir(path):
        raise NotADirectoryError(f"文件夹不存在: {path}")
    return path


def _move_to_trash(photo: dict[str, Any], config: Config) -> str:
    """Move a photo into a trash directory under its folder."""
    src = photo["path"]
    if not os.path.exists(src):
        raise FileNotFoundError(f"文件不存在: {src}")

    folder = photo["folder"] or os.path.dirname(src)
    trash_root = Path(folder) / config.trash_dir_name
    day_dir = trash_root / datetime.now().astimezone().strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    dest = day_dir / Path(src).name
    if dest.exists():
        dest = (
            day_dir
            / f"{Path(src).stem}_{uuid.uuid4().hex[:8]}{Path(src).suffix.lower()}"
        )
    shutil.move(src, str(dest))
    return str(dest)


def _restore_from_trash(photo: dict[str, Any]) -> str:
    """Move a deleted photo from trash back to its original path."""
    trash_path = photo["path"]
    original = photo.get("original_path") or trash_path
    if not os.path.exists(trash_path):
        raise FileNotFoundError(f"回收站文件不存在: {trash_path}")

    original = os.path.abspath(original)
    Path(original).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(original):
        stem = Path(original).stem
        suffix = Path(original).suffix
        original = str(Path(original).parent / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}")
    shutil.move(trash_path, original)
    return original


def _remove_photo_cache(photo: dict[str, Any]) -> None:
    """Delete cached proxy/thumbnail files for a photo, if present."""
    for key in ("thumb_path", "proxy_path"):
        path = photo.get(key)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.debug("photo cache file is already gone: %s", path)


def _ensure_exif(
    photo: dict[str, Any],
    database: Database,
    cfg: Config | None = None,
    resolve_location: bool = False,
) -> dict[str, Any]:
    """Backfill missing EXIF/location data for photos indexed by older versions.

    Location resolution is an online operation and is disabled by default in
    list endpoints; scans and single-photo re-analysis resolve it in the
    background pipeline instead.
    """
    if not photo.get("exif"):
        path = photo.get("path") or photo.get("original_path")
        if path and os.path.exists(path):
            try:
                location, exif = extract_exif(path)
            except (OSError, ValueError):
                location, exif = None, {}
            if location or exif:
                database.update_exif(photo["id"], location, exif)
                photo["location"] = location
                photo["exif"] = exif

    exif = photo.get("exif") or {}
    if (
        resolve_location
        and cfg
        and photo.get("location")
        and exif.get("latitude") is not None
        and exif.get("longitude") is not None
        and "，" not in str(photo.get("location"))
        and not any(ch in str(photo.get("location")) for ch in "省市县区镇")
    ):
        place = reverse_geocode(
            exif["latitude"],
            exif["longitude"],
            cfg.geocoding_provider,
            cfg.geocoding_api_key,
            interval=cfg.geocoding_interval,
        )
        if place:
            database.update_exif(photo["id"], place, exif)
            photo["location"] = place
    return photo


def _cleanup_cache(
    database: Database, cache_dir_name: str = ".photo-review-cache"
) -> dict[str, int]:
    """Delete unreferenced proxy files from every indexed folder's cache."""
    total_freed = 0
    total_size = 0
    total_photos = 0
    for folder in database.all_folders():
        result = cleanup_folder_cache(folder, database, cache_dir_name)
        total_freed += result["freed"]
        total_size += result["freed_size"]
        total_photos += result.get("photos_affected", 0)
    return {
        "freed": total_freed,
        "freed_size": total_size,
        "photos_affected": total_photos,
    }


def _load_settings_from_db(database: Database, cfg: Config) -> None:
    """Load persisted settings from the SQLite ``settings`` table.

    If the database has no settings yet (for example after the data directory
    was recreated), a local backup file outside ``data/`` is used as fallback.
    """
    raw = database.get_settings()
    if not raw:
        backup_path = (
            Path(__file__).resolve().parent.parent / "photo-review-settings.json"
        )
        if backup_path.exists():
            try:
                with open(backup_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                raw = {}
    for key in PERSISTED_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        current = getattr(cfg, key, None)
        try:
            if isinstance(current, bool):
                setattr(
                    cfg, key, str(value).strip().lower() in {"1", "true", "yes", "on"}
                )
            elif isinstance(current, int):
                setattr(cfg, key, int(float(str(value).strip())))
            elif isinstance(current, float):
                setattr(cfg, key, float(str(value).strip()))
            else:
                setattr(cfg, key, value)
        except (TypeError, ValueError):
            continue


def _save_settings_to_db(database: Database, cfg: Config) -> None:
    """Persist current config into the SQLite ``settings`` table.

    Also writes a small backup file outside ``data/`` so settings can survive
    accidental deletion of the data directory.
    """
    data = {key: getattr(cfg, key) for key in PERSISTED_FIELDS}
    database.save_settings(data)
    try:
        backup_path = (
            Path(__file__).resolve().parent.parent / "photo-review-settings.json"
        )
        backup_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    try:
        db_backup = Path(__file__).resolve().parent.parent / "photo-library.backup.db"
        database.backup_to(str(db_backup))
    except (OSError, sqlite3.Error):
        logger.warning("could not write database backup", exc_info=True)


def _load_legacy_cache_safely(
    folder: str, database: Database, cache_dir_name: str
) -> None:
    try:
        load_scan_results_from_cache(folder, database, cache_dir_name)
    except (OSError, ValueError, sqlite3.Error):
        logger.warning("could not import legacy cache for %s", folder, exc_info=True)


def create_app(config: Config | None = None, db: Database | None = None) -> Flask:
    from .config import CONFIG
    from .db import get_db

    cfg = config or CONFIG
    backup_path = Path(__file__).resolve().parent.parent / "photo-library.backup.db"
    if not os.path.exists(cfg.db_path) and backup_path.exists():
        try:
            shutil.copy2(backup_path, cfg.db_path)
        except OSError:
            pass
    database = db or get_db(cfg.db_path)
    _load_settings_from_db(database, cfg)
    static_dir = str(Path(__file__).resolve().parent.parent / "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    app.config["JSON_AS_ASCII"] = False

    @app.get("/")
    def index():
        return send_file(os.path.join(static_dir, "index.html"))

    @app.get("/api/config")
    def api_config():
        return jsonify(cfg.to_dict())

    @app.post("/api/config")
    def api_update_config():
        data = request.get_json(force=True, silent=True) or {}
        if "api_base_url" in data:
            cfg.api_base_url = str(data["api_base_url"]).strip() or cfg.api_base_url
        if "api_key" in data:
            cfg.api_key = str(data["api_key"])
        if "model" in data:
            cfg.model = str(data["model"]).strip() or cfg.model
        if "system_prompt" in data:
            cfg.system_prompt = str(data["system_prompt"])
        for key, minimum in (
            ("proxy_max_edge", 64),
            ("request_timeout", 5),
            ("model_retries", 0),
        ):
            if key in data:
                try:
                    setattr(cfg, key, max(minimum, int(data[key])))
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} 参数不合法"}), 400
        if "index_concurrency" in data:
            try:
                cfg.index_concurrency = max(1, min(32, int(data["index_concurrency"])))
            except (TypeError, ValueError):
                return jsonify({"error": "index_concurrency 参数不合法"}), 400
        if "geocoding_provider" in data:
            cfg.geocoding_provider = (
                str(data["geocoding_provider"]).strip() or "nominatim"
            )
        if "geocoding_api_key" in data:
            cfg.geocoding_api_key = str(data["geocoding_api_key"])
        if "geocoding_interval" in data:
            try:
                cfg.geocoding_interval = max(
                    0.1, min(10.0, float(data["geocoding_interval"]))
                )
            except (TypeError, ValueError):
                return jsonify({"error": "geocoding_interval 参数不合法"}), 400
        _save_settings_to_db(database, cfg)
        return jsonify(cfg.to_dict())

    @app.get("/api/prompt-template")
    def api_prompt_template():
        return jsonify({"template": SYSTEM_PROMPT})

    @app.get("/api/dirs")
    def api_dirs():
        path = request.args.get("path", "").strip()
        if not path:
            path = str(Path.home())
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            return jsonify({"error": "目录不存在"}), 404
        try:
            entries = sorted(os.listdir(path), key=str.lower)
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
        dirs = []
        for name in entries:
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
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            return jsonify({"error": "文件夹不存在"}), 404
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True})

    @app.post("/api/rebuild-index")
    def api_rebuild_index():
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip()
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        try:
            folder = _normalize_folder(folder)
        except NotADirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        for existing in JOBS.list():
            if existing.folder == folder and existing.status == "running":
                return jsonify(
                    {"error": "该文件夹已有任务正在运行", "job_id": existing.id}
                ), 409
        job = start_rebuild_index(folder, database, cfg)
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
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        try:
            folder = _normalize_folder(folder)
        except NotADirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        _load_legacy_cache_safely(folder, database, cfg.cache_dir_name)
        for existing in JOBS.list():
            if existing.folder == folder and existing.status == "running":
                return jsonify(
                    {"error": "该文件夹已有扫描任务正在运行", "job_id": existing.id}
                ), 409
        job = start_scan(folder, database, cfg, force=force)
        return jsonify({"job_id": job.id, "status": job.status, "folder": folder})

    @app.get("/api/scan/status/<job_id>")
    def api_scan_status(job_id: str):
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify(
            {
                "job_id": job.id,
                "folder": job.folder,
                "status": job.status,
                "total": job.total,
                "processed": job.processed,
                "current": job.current,
                "phase": job.phase,
                "error": job.error,
                "cancelled": job.cancelled,
            }
        )

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
        if folder:
            _load_legacy_cache_safely(folder, database, cfg.cache_dir_name)
        return jsonify({"years": database.capture_years(folder)})

    @app.get("/api/folders")
    def api_folders():
        return jsonify({"folders": database.all_folders()})

    @app.get("/api/photos")
    def api_photos():
        folder = request.args.get("folder", "").strip() or None
        status_raw = request.args.get("status", "").strip()
        recommendation = request.args.get("recommendation", "").strip() or None
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
        limit = min(int(request.args.get("limit", 1000)), 5000)
        offset = max(int(request.args.get("offset", 0)), 0)
        if folder:
            _load_legacy_cache_safely(folder, database, cfg.cache_dir_name)
        photos = database.list_photos(
            folder=folder,
            status=status,
            recommendation=recommendation,
            min_score=min_score,
            max_score=max_score,
            search=search,
            tag=tag,
            favorite=favorite,
            year=year,
            exclude_deleted=exclude_deleted,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        photos = [_ensure_exif(p, database, cfg) for p in photos]
        return jsonify({"photos": [_photo_payload(p) for p in photos]})

    @app.post("/api/search")
    def api_search():
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip() or None
        query = data.get("query", "").strip()
        mode = data.get("mode", "smart") or "smart"
        status_raw = data.get("status", "").strip()
        recommendation = data.get("recommendation", "").strip() or None
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
        if folder:
            _load_legacy_cache_safely(folder, database, cfg.cache_dir_name)

        all_candidates = database.list_photos(
            folder=folder,
            status=status,
            recommendation=recommendation,
            min_score=min_score,
            tag=tag,
            favorite=favorite,
            year=year,
            exclude_deleted=exclude_deleted,
            sort=sort,
            limit=2000,
        )
        result_map = {}

        if mode in ("keyword", "smart"):
            keyword_photos = database.list_photos(
                folder=folder,
                status=status,
                recommendation=recommendation,
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
        if mode in ("semantic", "smart") and query:
            try:
                ids = semantic_search(query, all_candidates, cfg)
                by_id = {p["id"]: p for p in all_candidates}
                for pid in ids:
                    if pid in by_id:
                        result_map[pid] = by_id[pid]
            except SemanticSearchError as exc:
                semantic_warning = str(exc)
                if mode == "semantic" and not result_map:
                    return jsonify({"error": semantic_warning}), 502

        photos = sort_photos(list(result_map.values()), sort)
        photos = [_ensure_exif(p, database, cfg) for p in photos]
        return jsonify(
            {
                "photos": [_photo_payload(p) for p in photos],
                "warning": semantic_warning,
            }
        )

    @app.get("/api/stats")
    def api_stats():
        folder = request.args.get("folder", "").strip() or None
        if folder:
            _load_legacy_cache_safely(folder, database, cfg.cache_dir_name)
        return jsonify(database.stats(folder))

    @app.get("/api/photo/<int:photo_id>")
    def api_photo(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        photo = _ensure_exif(photo, database, cfg)
        return jsonify(_photo_payload(photo))

    @app.get("/api/thumbnail/<int:photo_id>")
    def api_thumbnail(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        thumb = photo.get("thumb_path")
        if thumb and os.path.exists(thumb):
            return send_file(thumb, mimetype="image/jpeg", conditional=True)
        path = photo.get("path") or photo.get("original_path")
        if path and os.path.exists(path):
            try:
                folder = photo.get("folder") or os.path.dirname(path)
                thumb_dir = ensure_cache_dirs(folder, cfg.cache_dir_name)
                proxy_path, _, _, _ = make_proxy(
                    path,
                    max_edge=cfg.proxy_max_edge,
                    cache_dir=str(thumb_dir),
                    quality=cfg.proxy_quality,
                )
                database.update_cache_paths(photo_id, proxy_path)
                return send_file(proxy_path, mimetype="image/jpeg", conditional=True)
            except (OSError, ValueError):
                return jsonify({"error": "无法生成缩略图"}), 500
        return jsonify({"error": "缩略图不存在"}), 404

    @app.get("/api/original/<int:photo_id>")
    def api_original(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        path = photo.get("path") or photo.get("original_path")
        if not path or not os.path.exists(path):
            return jsonify({"error": "原图不存在"}), 404
        return send_file(path, conditional=True)

    @app.post("/api/delete")
    def api_delete():
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请选择要删除的照片"}), 400
        moved = []
        errors = []
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
            if photo.get("status") == "deleted":
                errors.append({"id": pid, "error": "照片已在回收站"})
                continue
            try:
                new_path = _move_to_trash(photo, cfg)
                database.mark_deleted([photo["id"]], {photo["id"]: new_path})
                moved.append({"id": photo["id"], "path": new_path})
                touched_folders.add(
                    photo.get("folder") or os.path.dirname(photo["path"])
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": pid, "error": str(exc)})
        for folder in touched_folders:
            try:
                cleanup_folder_cache(folder, database, cfg.cache_dir_name)
            except (OSError, sqlite3.Error):
                logger.warning("cache cleanup failed for %s", folder, exc_info=True)
        return jsonify({"moved": moved, "errors": errors})

    @app.post("/api/restore")
    def api_restore():
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请选择要恢复的照片"}), 400
        restored = []
        errors = []
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
                errors.append({"id": pid, "error": "照片不在回收站"})
                continue
            try:
                new_original = _restore_from_trash(photo)
                database.restore_deleted([photo["id"]], {photo["id"]: new_original})
                restored.append({"id": photo["id"], "path": new_original})
                touched_folders.add(
                    photo.get("folder")
                    or os.path.dirname(photo.get("original_path") or photo["path"])
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": pid, "error": str(exc)})
        for folder in touched_folders:
            try:
                cleanup_folder_cache(folder, database, cfg.cache_dir_name)
            except (OSError, sqlite3.Error):
                logger.warning("cache cleanup failed for %s", folder, exc_info=True)
        return jsonify({"restored": restored, "errors": errors})

    @app.post("/api/delete/permanent")
    def api_delete_permanent():
        data = request.get_json(force=True, silent=True) or {}
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请选择要永久删除的照片"}), 400
        purged = []
        errors = []
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
                try:
                    os.remove(path)
                    file_removed = True
                except OSError as exc:
                    errors.append({"id": pid, "error": f"删除文件失败: {exc}"})
                    continue
            touched_folders.add(
                photo.get("folder")
                or os.path.dirname(photo.get("original_path") or photo["path"])
            )
            _remove_photo_cache(photo)
            database.delete_rows([pid])
            purged.append({"id": pid, "file_removed": file_removed})
        for folder in touched_folders:
            try:
                cleanup_folder_cache(folder, database, cfg.cache_dir_name)
            except (OSError, sqlite3.Error):
                logger.warning("cache cleanup failed for %s", folder, exc_info=True)
        return jsonify({"purged": purged, "errors": errors})

    @app.post("/api/folder/remove")
    def api_folder_remove():
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip()
        if not folder:
            return jsonify({"error": "请提供文件夹路径"}), 400
        folder = os.path.abspath(os.path.expanduser(folder))
        for existing in JOBS.list():
            if existing.folder == folder and existing.status == "running":
                return jsonify({"error": "该文件夹正在整理中，请稍后再试"}), 409
        removed = database.remove_folder(folder)
        cache_dir = cache_dir_for_folder(folder, cfg.cache_dir_name)
        cache_removed = False
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_removed = not cache_dir.exists()
        return jsonify({"removed": removed, "cache_removed": cache_removed})

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
        dimensions = dict(photo.get("dimensions") or {})
        raw_dimensions = data.get("dimensions") or {}
        if isinstance(raw_dimensions, dict):
            for key in ("technical", "composition", "memory", "uniqueness"):
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
        data = request.get_json(force=True, silent=True) or {}
        folder = data.get("folder", "").strip() or None
        if folder:
            folder = os.path.abspath(os.path.expanduser(folder))
        photos = database.list_photos(
            folder=folder,
            status="deleted",
            exclude_deleted=False,
            sort="filename",
            limit=100000,
        )
        deleted = 0
        errors = []
        touched_folders = set()
        for photo in photos:
            candidates = []
            if photo.get("path"):
                candidates.append(photo["path"])
            original = photo.get("original_path")
            if original and original not in candidates:
                candidates.append(original)
            for candidate in candidates:
                if candidate and os.path.exists(candidate):
                    try:
                        os.remove(candidate)
                    except OSError as exc:
                        errors.append(
                            {"id": photo["id"], "error": f"删除文件失败: {exc}"}
                        )
                        break
            else:
                _remove_photo_cache(photo)
                database.delete_rows([photo["id"]])
                deleted += 1
                touched_folders.add(
                    photo.get("folder") or os.path.dirname(photo.get("path") or "")
                )
        for touched in touched_folders:
            try:
                cleanup_folder_cache(touched, database, cfg.cache_dir_name)
            except (OSError, sqlite3.Error):
                logger.warning("cache cleanup failed for %s", touched, exc_info=True)
        return jsonify({"deleted": deleted, "errors": errors})

    @app.post("/api/cache/clean")
    def api_cache_clean():
        data = request.get_json(force=True, silent=True) or {}
        folder = request.args.get("folder", "").strip() or data.get("folder", "")
        if folder:
            folder = os.path.abspath(os.path.expanduser(folder))
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

    return app
