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
import socket

from photo_reviewer.config import CONFIG
from photo_reviewer.server import create_app

#: How many ports to try after the configured one before giving up.
PORT_ATTEMPTS = 40

app = create_app(CONFIG)


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
        print(f"拾光相册已启动： http://{host}:{port}", flush=True)
        # ``use_reloader`` stays off: it re-runs this module in a child process,
        # which would probe and bind a second time.
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
