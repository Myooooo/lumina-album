"""Flask application factory for the local photo review system.

The routes live in :mod:`photo_reviewer.web` and register themselves on the app;
this module owns the Flask setup, the cross-origin guard and the shared
configuration/database context they receive.
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path

from flask import Flask, jsonify, request

from .config import Config
from .db import Database
from .web.constants import _UNSAFE_METHODS
from .web.helpers import (
    _load_settings_from_db,
    _prune_legacy_settings,
    _start_background_repairs,
)

logger = logging.getLogger(__name__)

_LAN_READONLY_ALLOWED_POSTS = frozenset({"/api/search", "/api/cull/quality", "/api/export"})
_LAN_READONLY_BLOCKED_PREFIXES = ("/api/dirs", "/api/config", "/api/folders/hidden")


def create_app(config: Config | None = None, db: Database | None = None) -> Flask:
    from .config import CONFIG
    from .db import get_db

    cfg = config or CONFIG
    database = db or get_db(cfg.db_path)
    _prune_legacy_settings(database)
    _load_settings_from_db(database, cfg)
    _start_background_repairs(database, cfg)
    static_dir = str(Path(__file__).resolve().parent.parent / "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    app.config["JSON_AS_ASCII"] = False

    @app.before_request
    def _lan_readonly_guard():
        """When PHOTO_LAN_READONLY_PIN is set, enforce read-only + PIN for non-loopback clients."""
        pin = (getattr(cfg, "lan_readonly_pin", "") or "").strip()
        if not pin:
            return None
        remote = (request.remote_addr or "").strip()
        if remote in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
            return None
        supplied = (
            request.headers.get("X-Lumina-Pin")
            or request.args.get("pin")
            or request.cookies.get("lumina_lan_pin")
            or ""
        ).strip()
        if not supplied or not hmac.compare_digest(supplied, pin):
            return jsonify({"error": "局域网访问需要口令（请附加 ?pin=口令 或 X-Lumina-Pin 请求头）"}), 401
        if any(
            request.path == prefix or request.path.startswith(prefix + "/")
            for prefix in _LAN_READONLY_BLOCKED_PREFIXES
        ):
            return jsonify({"error": "局域网只读模式不允许访问宿主机配置或目录接口"}), 403
        if request.method in _UNSAFE_METHODS and request.path not in _LAN_READONLY_ALLOWED_POSTS:
            return jsonify({"error": "局域网只读模式不允许修改或删除操作"}), 403
        return None

    @app.after_request
    def _persist_lan_pin_cookie(response):
        pin = (getattr(cfg, "lan_readonly_pin", "") or "").strip()
        query_pin = (request.args.get("pin") or "").strip()
        if pin and query_pin and hmac.compare_digest(query_pin, pin):
            response.set_cookie("lumina_lan_pin", pin, httponly=True, samesite="Lax")
        return response

    @app.before_request
    def _reject_cross_origin_writes():
        """Block CSRF on every mutating route.

        Flask parses a JSON body even when the browser sends it as
        ``text/plain``, which makes ordinary form posts and ``fetch`` calls from
        any other page able to drive ``/api/delete`` and friends. Requiring the
        browser-declared origin to match this host closes that hole without
        tokens or sessions; requests without an ``Origin``/``Referer`` header
        (curl, the test client) are still allowed.
        """
        if request.method not in _UNSAFE_METHODS:
            return None
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if not source:
            return None
        host = request.host
        if source in (f"http://{host}", f"https://{host}"):
            return None
        allowed = cfg.allowed_origins
        base = source.split("/", 3)
        if allowed and len(base) >= 3 and f"{base[0]}//{base[2]}" in allowed:
            return None
        logger.warning(
            "rejected cross-origin %s %s from %s", request.method, request.path, source
        )
        return jsonify({"error": "跨站请求已被拒绝"}), 403

    # Each group registers itself; the app factory only owns the Flask setup,
    # the request guard and the shared context.
    from .web import (
        routes_config,
        routes_deletion,
        routes_files,
        routes_index,
        routes_jobs,
        routes_media,
        routes_people,
        routes_photos,
        routes_sheets,
    )

    for module in (
        routes_index,
        routes_config,
        routes_files,
        routes_jobs,
        routes_people,
        routes_sheets,
        routes_photos,
        routes_media,
        routes_deletion,
    ):
        module.register(app, cfg, database)

    return app
