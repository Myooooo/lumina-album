"""Path helpers shared by the scanner, the pipeline and the web layer.

Windows treats ``E:\\Photos`` and ``e:\\photos`` as the same directory, but
SQLite string comparison does not. Without canonicalisation the same folder can
be indexed twice under two spellings, and the gallery then shows an empty album
for whichever spelling the user happens to type. ``canonical_path`` resolves a
path to the on-disk spelling so every stored path compares equal.
"""

from __future__ import annotations

import os


def canonical_path(path: str | None) -> str:
    """Return the canonical absolute form of ``path``.

    The result is what ``os.path.realpath`` reports on this filesystem, which
    normalises the case of every existing component on Windows. Non-existent
    paths (trash entries, deleted originals) are still resolved lexically, so
    the function is safe to call on paths that are not on disk.
    """
    if not path:
        return ""
    expanded = os.path.expanduser(str(path).strip())
    if not expanded:
        return ""
    return os.path.realpath(os.path.abspath(expanded))


def canonical_folder(path: str | None) -> str:
    """Canonicalise a photo folder, raising when it is not a directory."""
    resolved = canonical_path(path)
    if not resolved or not os.path.isdir(resolved):
        raise NotADirectoryError(f"文件夹不存在: {resolved or path}")
    return resolved


def same_path(left: str | None, right: str | None) -> bool:
    """Compare two paths the way the filesystem does."""
    if not left or not right:
        return False
    return os.path.normcase(canonical_path(left)) == os.path.normcase(
        canonical_path(right)
    )
