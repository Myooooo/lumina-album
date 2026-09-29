"""Thumbnail/proxy generation and perceptual hashing."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

# Kept in sync with ``Config.raw_extensions``. Duplicated here so the
# thumbnailer stays free of configuration imports.
RAW_EXTENSIONS = frozenset(
    {
        ".nef",
        ".arw",
        ".cr2",
        ".cr3",
        ".nrw",
        ".dng",
        ".orf",
        ".rw2",
        ".raf",
        ".pef",
        ".srw",
        ".raw",
        ".rwl",
        ".3fr",
        ".iiq",
        ".mos",
        ".mrw",
        ".k25",
        ".kdc",
        ".dcr",
        ".x3f",
        ".erf",
        ".mef",
        ".sr2",
        ".srf",
        ".cap",
        ".fff",
    }
)

# A stream smaller than this is treated as a stub, not a viewable preview.
_MIN_PREVIEW_BYTES = 4096


def _safe_hash(path: str) -> str:
    """Hash file path + mtime + size to use as a stable cache key."""
    try:
        st = os.stat(path)
        key = f"{path}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        key = path
    return hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:20]


class _BytesReader:
    """Minimal seekable file-like wrapper around an in-memory buffer."""

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        else:
            self._pos = len(self._data) + offset
        self._pos = max(0, self._pos)
        return self._pos

    def tell(self) -> int:
        return self._pos


def _embedded_jpeg_streams(path: str, max_scan: int = 96 * 1024 * 1024) -> list:
    """Collect every complete JPEG stream (SOI..EOI) inside a raw container."""
    with open(path, "rb") as fh:
        data = fh.read(max_scan)

    streams = []
    pos = 0
    while True:
        start = data.find(b"\xff\xd8\xff", pos)
        if start < 0:
            break
        end = data.find(b"\xff\xd9", start + 3)
        if end < 0:
            break
        pos = end + 2
        if end + 2 - start >= _MIN_PREVIEW_BYTES:
            streams.append(data[start : end + 2])
    return streams


def extract_raw_preview(image_path: str) -> Image.Image | None:
    """Return the largest embedded JPEG preview of a raw file, as RGB.

    Camera raw containers store a full-size JPEG next to the sensor data. That
    preview is what we render: it needs no demosaicing library and is what the
    camera itself considers the finished picture.

    Returns ``None`` when the container holds no usable preview or cannot be
    read, so callers can fall back to Pillow's own reader.
    """
    try:
        candidates = _embedded_jpeg_streams(image_path)
    except OSError:
        return None

    best = None
    best_pixels = 0
    for blob in candidates:
        try:
            with Image.open(_BytesReader(blob)) as img:
                img.draft("RGB", (4096, 4096))
                img.load()
                pixels = img.size[0] * img.size[1]
                if pixels <= best_pixels:
                    continue
                best_pixels = pixels
                best = ImageOps.exif_transpose(img).convert("RGB")
        except (OSError, ValueError, UnidentifiedImageError):
            continue
    return best


def open_source_image(image_path: str) -> Image.Image:
    """Open a photo or camera-raw file and return a usable image object.

    Most raw containers either cannot be opened by Pillow at all (Sony ``.arw``,
    Adobe ``.dng``) or open as a tiny placeholder rather than the real picture
    (Nikon ``.nef`` reports 160x120). For those the embedded preview is used
    instead. Non-raw files are opened normally.

    The caller owns the returned object and should close it. For the preview
    branch the image is already detached from the file, so closing it is a
    no-op for the file handle.
    """
    image_path = os.path.abspath(image_path)
    if Path(image_path).suffix.lower() in RAW_EXTENSIONS:
        preview = extract_raw_preview(image_path)
        if preview is not None:
            return preview
    return Image.open(image_path)


def make_proxy(
    image_path: str,
    max_edge: int = 1024,
    cache_dir: str = "data/thumbnails",
    quality: int = 85,
    thumb_size: int = 480,
    thumb_quality: int = 50,
) -> tuple[str, int, int, str, str]:
    """Create a JPEG proxy and a small gallery thumbnail for one photo.

    Returns ``(proxy_path, width, height, image_hash, thumb_path)``. The proxy
    is sent to the vision API and doubles as the large preview; the small
    thumbnail is served to the gallery grid so large albums do not download
    full-size proxies.

    ``image_hash`` is a 64-bit difference hash (dHash) of the image, stored in
    the ``phash`` column; it is not a DCT-based pHash.

    Both files are written with a temporary name and moved into place, so a
    concurrent reader (or a crash) never observes a half-written JPEG.
    """
    image_path = os.path.abspath(image_path)
    cache_dir = os.path.abspath(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)

    with open_source_image(image_path) as img:
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        width, height = img.size
        base = _safe_hash(image_path)

        proxy_path = os.path.join(cache_dir, f"{base}_proxy.jpg")
        if not os.path.exists(proxy_path):
            proxy_img = img.copy()
            if max(proxy_img.size) > max_edge:
                proxy_img.thumbnail((max_edge, max_edge), Image.LANCZOS)
            _save_jpeg_atomic(proxy_img, proxy_path, quality)

        thumb_path = os.path.join(cache_dir, f"{base}_thumb.jpg")
        if not os.path.exists(thumb_path):
            thumb_img = img.copy()
            if max(thumb_img.size) > thumb_size:
                thumb_img.thumbnail((thumb_size, thumb_size), Image.LANCZOS)
            _save_jpeg_atomic(thumb_img, thumb_path, thumb_quality)

        image_hash = _dhash(img, hash_size=8)
        return proxy_path, width, height, image_hash, thumb_path


def _save_jpeg_atomic(img: Image.Image, path: str, quality: int) -> None:
    temp_path = f"{path}.{os.getpid()}.{id(img)}.tmp"
    try:
        img.save(temp_path, "JPEG", quality=quality)
        os.replace(temp_path, path)
    except OSError:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def _dhash(img: Image.Image, hash_size: int = 8) -> str:
    """Compute a difference hash (dHash) as a fixed-width hex string."""
    gray = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(gray.getdata())
    bits = []
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            bits.append("1" if left > right else "0")
    return format(int("".join(bits), 2), f"0{hash_size * hash_size // 4}x")


def _popcount(value: int) -> int:
    """Population count that also works on Python 3.9 (no ``int.bit_count``)."""
    bit_count = getattr(int, "bit_count", None)
    if bit_count is not None:
        return bit_count(value)
    total = 0
    while value:
        total += _POPCOUNT_BYTE[value & 0xFF]
        value >>= 8
    return total


_POPCOUNT_BYTE = bytes(format(i, "b").count("1") for i in range(256))


def hamming_distance(a: str, b: str) -> int:
    """Hamming distance between two hex dHash strings.

    Returns 999 when either value is missing or not hexadecimal, and when the
    two hashes do not describe the same bit width (a legacy variable-width
    hash is not comparable).
    """
    if not a or not b:
        return 999
    try:
        ia = int(a, 16)
        ib = int(b, 16)
    except (TypeError, ValueError):
        return 999
    if len(a.strip()) != len(b.strip()):
        return 999
    return _popcount(ia ^ ib)


def cleanup_cache(referenced_paths, thumb_dir: str) -> dict:
    """Delete cached proxy/thumbnail files that are not referenced by the DB.

    ``referenced_paths`` is an iterable of absolute paths that must be kept.
    Leftover ``*.tmp`` files from interrupted writes are swept as well. Returns
    ``{"freed": int, "freed_size": int, "photos_affected": int}``.
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
        if name.endswith(".tmp"):
            try:
                os.remove(os.path.join(thumb_dir, name))
            except OSError:
                pass
            continue
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
