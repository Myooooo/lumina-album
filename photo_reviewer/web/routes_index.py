"""Index and favicon.

Registered by :func:`photo_reviewer.server.create_app`; the route bodies are
grouped by the part of the app they serve.
"""

from __future__ import annotations

import os

from flask import send_file


def register(app, cfg, database):
    """Register these routes on ``app``."""

    static_dir = app.static_folder

    @app.get("/")
    def index():
        return send_file(os.path.join(static_dir, "index.html"))

    @app.get("/favicon.ico")
    @app.get("/favicon.svg")
    def favicon():
        return send_file(
            os.path.join(static_dir, "favicon.svg"), mimetype="image/svg+xml"
        )
