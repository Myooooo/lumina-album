#!/usr/bin/env python3
"""Entry point for the Local Photo Review System.

Run:
    python app.py
Then open http://127.0.0.1:5000
"""
from photo_reviewer.config import CONFIG
from photo_reviewer.server import create_app

app = create_app(CONFIG)

if __name__ == "__main__":
    app.run(host=CONFIG.host, port=CONFIG.port, debug=CONFIG.debug, threaded=True)
