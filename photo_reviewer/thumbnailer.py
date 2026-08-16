"""Thumbnail/proxy generation and perceptual hashing."""

from __future__ import annotations

import hashlib
import os

from PIL import Image, ImageOps


def _safe_hash(path: str) -> str:
    """Hash file path + mtime + size to use as a stable cache key."""
    try:
        st = os.stat(path)
        key = f"{path}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        key = path
    return hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:20]


def make_proxy(
    image_path: str,
    max_edge: int = 1024,
    cache_dir: str = "data/thumbnails",
    quality: int = 85,
    thumb_size: int = 480,
    thumb_quality: int = 50,
) -> tuple[str, int, int, str, str]:
    """Create a JPEG proxy and a small gallery thumbnail for one photo.

    Returns (proxy_path, width, height, proxy_hash, thumb_path). The proxy is
    sent to the vision API; the small thumbnail is served to the gallery so
    large albums do not download full-size proxies.
    """
    image_path = os.path.abspath(image_path)
    cache_dir = os.path.abspath(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)

    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        width, height = img.size
        base = _safe_hash(image_path)

        # Proxy: keep aspect ratio, limit the longest edge for API upload.
        # The same proxy file is also used directly as the gallery preview,
        # so no separate thumbnail file is created.
        proxy_path = os.path.join(cache_dir, f"{base}_proxy.jpg")
        if not os.path.exists(proxy_path):
            proxy_img = img.copy()
            if max(proxy_img.size) > max_edge:
                proxy_img.thumbnail((max_edge, max_edge), Image.LANCZOS)
            proxy_img.save(proxy_path, "JPEG", quality=quality)

        thumb_path = os.path.join(cache_dir, f"{base}_thumb.jpg")
        if not os.path.exists(thumb_path):
            thumb_img = img.copy()
            if max(thumb_img.size) > thumb_size:
                thumb_img.thumbnail((thumb_size, thumb_size), Image.LANCZOS)
            thumb_img.save(thumb_path, "JPEG", quality=thumb_quality)

        proxy_hash = _dhash(img, hash_size=8)
        return proxy_path, width, height, proxy_hash, thumb_path


def _dhash(img: Image.Image, hash_size: int = 8) -> str:
    """Compute a difference hash (dHash) as a hex string."""
    gray = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(gray.getdata())
    bits = []
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            bits.append("1" if left > right else "0")
    return hex(int("".join(bits), 2))[2:].zfill(hash_size * hash_size // 4)


def hamming_distance(a: str, b: str) -> int:
    """Hamming distance between two hex dHash strings."""
    if not a or not b:
        return 999
    try:
        ia = int(a, 16)
        ib = int(b, 16)
    except ValueError:
        return 999
    return (ia ^ ib).bit_count()


def cleanup_cache(referenced_paths, thumb_dir: str) -> dict:
    """Delete cached proxy/thumbnail files that are not referenced by the DB.

    ``referenced_paths`` is an iterable of absolute paths that must be kept.
    Returns {"freed": int, "freed_size": int}.
    """
    referenced = set()
    for p in referenced_paths:
        if p:
            referenced.add(os.path.abspath(p))

    freed = 0
    freed_size = 0
    affected_bases = set()
    thumb_dir = os.path.abspath(thumb_dir)
    if not os.path.isdir(thumb_dir):
        return {"freed": freed, "freed_size": freed_size, "photos_affected": 0}

    for name in os.listdir(thumb_dir):
        if not name.endswith(("_thumb.jpg", "_proxy.jpg")):
            continue
        path = os.path.join(thumb_dir, name)
        if path in referenced:
            continue
        try:
            freed_size += os.path.getsize(path)
            os.remove(path)
            freed += 1
            if name.endswith("_proxy.jpg"):
                affected_bases.add(name[: -len("_proxy.jpg")])
            elif name.endswith("_thumb.jpg"):
                affected_bases.add(name[: -len("_thumb.jpg")])
        except OSError:
            pass
    return {
        "freed": freed,
        "freed_size": freed_size,
        "photos_affected": len(affected_bases),
    }
