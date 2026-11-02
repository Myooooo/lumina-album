#!/usr/bin/env python3
"""Entry point for the Local Photo Review System.

Run:
    python app.py
Then open the URL it prints.

The port comes from ``PHOTO_PORT`` (default 5050). A port can be unavailable for
two very different reasons: something is already listening on it, or Windows has
reserved it for itself. The second case is common - Windows hands out blocks of
ephemeral ports (``netsh interface ipv4 show excludedportrange protocol=tcp``)
and any server that tries to bind one gets WinError 10013, "an attempt was made
to access a socket in a way forbidden by its access permissions", which says
nothing about what to do next.

Either way the fix is the same, so the port is probed before the server starts:
the first one that binds is the one used, and the URL is printed so it is
obvious where to point the browser.
"""

import errno
import os
import socket
import threading
import webbrowser

from photo_reviewer.config import CONFIG
from photo_reviewer.server import create_app

#: How many ports to try after the configured one before giving up.
PORT_ATTEMPTS = 40

#: How long to wait before opening the browser. The server starts listening a
#: moment after ``app.run`` is entered, and a browser that arrives before that
#: shows the "can't connect" page and has to be refreshed by hand.
OPEN_BROWSER_DELAY = 1.0

app = create_app(CONFIG)


def _browser_disabled() -> bool:
    """Whether the user asked for no browser window (headless, tests, servers)."""
    value = os.getenv("PHOTO_OPEN_BROWSER", "").strip().lower()
    return value in {"0", "false", "no", "off"}


def open_browser_later(url: str) -> None:
    """Open ``url`` in the default browser once the server is listening.

    A timer thread rather than a call before ``app.run``: the bind happens inside
    ``app.run``, so opening first would race it. Daemon so it never keeps the
    process alive, and every failure is swallowed - a machine with no browser
    (or a headless session) must still serve the app, with the URL printed as
    before.
    """
    if _browser_disabled():
        return

    def worker() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - no browser is not a fatal error
            # Headless sessions and machines with no default handler land here.
            # The URL is already printed, so there is nothing to recover from.
            print(f"没能自动打开浏览器，请手动访问： {url}", flush=True)

    timer = threading.Timer(OPEN_BROWSER_DELAY, worker)
    timer.daemon = True
    timer.start()


def _port_is_free(host: str, port: int) -> bool:
    """Try to bind ``port`` and immediately let it go.

    The socket options mirror what Werkzeug uses, so a port that passes here is
    one the server can actually take. ``SO_REUSEADDR`` is deliberately not set:
    on Windows it lets a second process claim a port that is already being
    listened on, which would turn "somebody else is using this" into a confusing
    half-bound server instead of a clean "try the next one".
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES) or getattr(
            exc, "winerror", None
        ) == 10013:
            return False
        raise
    finally:
        probe.close()
    return True


def main() -> int:
    host = CONFIG.host
    for offset in range(PORT_ATTEMPTS):
        port = CONFIG.port + offset
        if not _port_is_free(host, port):
            if offset == 0:
                print(f"端口 {port} 被占用或已被系统保留，正在寻找下一个可用端口…", flush=True)
            continue
        if offset:
            print(f"改用端口 {port}（原端口 {CONFIG.port} 不可用）。", flush=True)
        url = f"http://{host}:{port}"
        print(f"拾光相册已启动： {url}", flush=True)
        open_browser_later(url)
        # ``use_reloader`` stays off: it re-runs this module in a child process,
        # which would probe and bind a second time - and would open a second
        # browser tab.
        app.run(
            host=host,
            port=port,
            debug=CONFIG.debug,
            threaded=True,
            use_reloader=False,
        )
        return 0
    print(
        f"在 {CONFIG.port}-{CONFIG.port + PORT_ATTEMPTS - 1} 之间找不到可用端口。\n"
        "可以用环境变量指定一个，例如：set PHOTO_PORT=8080",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
