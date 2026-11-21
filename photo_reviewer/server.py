"""Flask application factory for the local photo review system.

The routes live in :mod:`photo_reviewer.web` and register themselves on the app;
this module owns the Flask setup, the cross-origin guard and the shared
configuration/database context they receive.
"""

from __future__ import annotations

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
        routes_photos,
        routes_sheets,
    )

    for module in (
        routes_index,
        routes_config,
        routes_files,
        routes_jobs,
        routes_sheets,
        routes_photos,
        routes_media,
        routes_deletion,
    ):
        module.register(app, cfg, database)

    return app
