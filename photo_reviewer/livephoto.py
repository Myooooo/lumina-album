"""Motion photos and Live Photos: the video hidden inside a still.

Three families of "live" media end up in a photo folder, and all three look
like an ordinary still until you know where to look:

* **Motion Photo 1.0** (current Android: Pixel, Samsung, Oppo, Vivo, Honor...).
  A JPEG/HEIC/AVIF with an MP4 appended. The XMP block carries a
  ``Container:Directory`` listing each item's MIME type and length; the video is
  the last item. Specification:
  https://developer.android.com/media/platform/motion-photo-format
* **Micro Video v1** (older Pixels, some Xiaomi builds). Same idea, older
  metadata: ``Camera:MicroVideoOffset`` is the distance from the *end* of the
  file back to the first byte of the video.
* **Samsung / unbranded trailers**. The XMP confirms a video exists but does not
  say how long it is, so the appended MP4 has to be found by reading the tail
  and looking for a self-consistent ISO-BMFF box.

On top of that sit **Live Photos**: an iPhone writes the still and the video as
two files (``IMG_1234.HEIC`` + ``IMG_1234.MOV``), so there is nothing to slice -
the pairing just has to be noticed.

Everything here is pure byte inspection over ``open()``/``seek()``. Nothing
decodes an image or runs ffmpeg, so it is cheap enough to run for every photo
during indexing, and it can be unit-tested with hand-built fixtures.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from .video import VIDEO_EXTENSIONS

#: How much of the head to read looking for the XMP packet. JPEG APP1 segments
#: holding XMP almost always sit within the first few kilobytes, but panorama
#: tools sometimes append a second packet later on.
_XMP_HEAD_BYTES = 128 * 1024

#: Progressive tail reads when the length is not known up front. A phone motion
#: photo is usually 1-4 MB; a long Samsung clip can be larger.
_TAIL_STEPS = (4 * 1024 * 1024, 16 * 1024 * 1024, 64 * 1024 * 1024)

#: Stills that can carry an embedded video.
MOTION_STILL_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".jpe", ".heic", ".heif", ".avif", ".webp"}
)

#: Anything smaller cannot hold a still plus a movie, so it is not worth probing.
_MIN_STILL_BYTES = 512

#: The filename convention Motion Photo 1.0 recommends: ``IMG_0001MP.jpg``.
_MOTION_NAME = re.compile(r"(?:^|[^a-z])mp$", re.IGNORECASE)

_XMP_MARKERS = (
    b"MotionPhoto",
    b"MicroVideo",
    b"Container:Directory",
    b"GContainer",
    b"VideoLength",
)


@dataclass
class MotionSource:
    """Where the video inside a still lives."""

    #: ``motion-photo`` / ``micro-video`` / ``trailer`` / ``mpvd`` / ``paired``.
    kind: str
    #: For an embedded video: the byte range inside the still.
    start: int = 0
    length: int = 0
    #: For an iPhone pair: the separate video file.
    video_path: str | None = None
    #: Total size of the container file, for diagnostics.
    file_size: int = 0

    @property
    def embedded(self) -> bool:
        return self.video_path is None and self.length > 0


def _read(path: str, offset: int, size: int) -> bytes:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, offset))
            return fh.read(size)
    except OSError:
        return b""


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_xmp(path: str) -> str:
    """Return the XMP packet(s) from the head of the file, or ``""``.

    Only the head is scanned: that is where a JPEG's APP1 segment and a HEIF's
    ``mime`` item live. A file whose packet sits further in simply reads as "no
    metadata", which falls back to the byte-level trailer check.
    """
    head = _read(path, 0, _XMP_HEAD_BYTES)
    if not head:
        return ""
    if not any(marker in head for marker in _XMP_MARKERS):
        return ""
    text = head.decode("latin-1", errors="replace")
    start = text.find("<x:xmpmeta")
    end = text.rfind("</x:xmpmeta>")
    if start >= 0 and end > start:
        return text[start : end + len("</x:xmpmeta>")]
    # Not a well-formed packet, but the attributes are still worth reading.
    return text


def _attributes(fragment: str, names: tuple[str, ...]) -> dict[str, str]:
    """Pull ``Name="value"`` pairs out of an XML attribute list."""
    found: dict[str, str] = {}
    for name in names:
        match = re.search(rf'{re.escape(name)}\s*=\s*"([^"]*)"', fragment)
        if match:
            found[name] = match.group(1)
    return found


def parse_container_items(xmp: str) -> list[dict[str, str]]:
    """Read ``Container:Directory`` into a list of item attribute dicts.

    Each ``Item`` element carries its own ``Item:Mime``, ``Item:Semantic``,
    ``Item:Length`` and ``Item:Padding``; walking them in order is what makes it
    possible to add up the lengths that precede the video.
    """
    if "Container:Directory" not in xmp:
        return []
    items: list[dict[str, str]] = []
    for match in re.finditer(r"Item:Mime\s*=\s*\"([^\"]*)\"", xmp):
        window = xmp[match.start() : match.start() + 600]
        attributes = _attributes(
            window,
            (
                "Item:Mime",
                "Item:Semantic",
                "Item:Length",
                "Item:Padding",
            ),
        )
        attributes.setdefault("Item:Mime", match.group(1))
        items.append(attributes)
    return items


def _is_motion_xmp(xmp: str) -> bool:
    """Does the packet claim this still carries a video?

    The specification is explicit that ``Camera:MotionPhoto="1"`` survives
    editors that strip the appended video, so this is only ever a hint - the
    byte check still has to succeed.
    """
    for key in ("Camera:MotionPhoto", "MotionPhoto"):
        match = re.search(rf'{key}\s*=\s*"(-?\d+)"', xmp)
        if match and int(match.group(1)) > 0:
            return True
    for key in ("Camera:MicroVideo",):
        match = re.search(rf'{key}\s*=\s*"(-?\d+)"', xmp)
        if match and int(match.group(1)) > 0:
            return True
    return "Container:Directory" in xmp and "MotionPhoto" in xmp


def _micro_video_offset(xmp: str) -> int | None:
    match = re.search(r'Camera:MicroVideoOffset\s*=\s*"(-?\d+)"', xmp)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def _motion_video_length(xmp: str) -> int | None:
    """Length of the video item, when the Container directory states it."""
    items = parse_container_items(xmp)
    for item in items:
        semantic = item.get("Item:Semantic", "").lower()
        if semantic == "motionphoto" or item.get("Item:Mime", "").startswith("video/"):
            try:
                length = int(item.get("Item:Length", "0"))
            except ValueError:
                continue
            if length > 0:
                return length
    return None


def _looks_like_mp4(data: bytes) -> bool:
    """A slice starting at a box whose type is ``ftyp``."""
    return len(data) >= 8 and data[4:8] == b"ftyp"


def _printable_box_type(raw: bytes) -> bool:
    return len(raw) == 4 and all(0x20 <= byte <= 0x7E for byte in raw)


def _box_chain_length(data: bytes, start: int) -> int | None:
    """Walk a run of ISO-BMFF boxes and return the offset where it ends.

    An MP4 is a flat sequence of ``(size, type)`` boxes. Requiring the sequence
    to be well formed - every size at least eight bytes, every type printable,
    no gap and no overflow - is what distinguishes the real appended video from
    a stray ``ftyp`` string inside the JPEG (a thumbnail's header, say). Returns
    ``None`` as soon as the chain stops making sense.
    """
    limit = len(data)
    cursor = start
    boxes = 0
    while cursor + 8 <= limit:
        size = struct.unpack(">I", data[cursor : cursor + 4])[0]
        box_type = data[cursor + 4 : cursor + 8]
        if not _printable_box_type(box_type):
            return None
        if size == 1:
            if cursor + 16 > limit:
                return None
            size = struct.unpack(">Q", data[cursor + 8 : cursor + 16])[0]
            if size < 16:
                return None
        elif size == 0:
            # "To end of file", only legal for the last box.
            return limit
        elif size < 8:
            return None
        if cursor + size > limit:
            return None
        cursor += size
        boxes += 1
        if boxes > 4096:
            return None
    # A chain that ends exactly at the buffer end is a complete file.
    return cursor if cursor == limit else None


def _find_trailer_mp4(path: str, file_size: int) -> tuple[int, int] | None:
    """Locate an MP4 appended to the end of a still.

    Samsung and some vendor builds append the MP4 without recording its length,
    so the position has to be recovered from the bytes: find an ``ftyp`` box and
    check that the boxes from there run cleanly to end-of-file. The earliest
    such position wins, because the appended movie may start with a small vendor
    box (Samsung writes a ``MotionPhoto_Data`` header) before its ``ftyp``.
    """
    for tail in _TAIL_STEPS:
        tail = min(tail, file_size)
        start = max(0, file_size - tail)
        data = _read(path, start, tail)
        if not data:
            continue
        candidates: list[int] = []
        index = data.find(b"ftyp")
        while index >= 0:
            box_start = index - 4
            if box_start >= 0:
                declared = struct.unpack(">I", data[box_start : box_start + 4])[0]
                # A real ftyp box is a couple of dozen bytes, never megabytes.
                if 8 <= declared <= 4096:
                    candidates.append(box_start)
            index = data.find(b"ftyp", index + 4)
        # Give the vendor header a chance: try up to 64 bytes before each ftyp.
        for candidate in sorted(candidates):
            for back in (0, 8, 16, 24, 32, 48, 64):
                probe = candidate - back
                if probe < 0:
                    continue
                if _box_chain_length(data, probe) is not None:
                    return start + probe, file_size - (start + probe)
        if tail >= file_size:
            break
    return None


def _find_mpvd(path: str, file_size: int) -> tuple[int, int] | None:
    """Locate the ``mpvd`` box a HEIC/AVIF motion photo ends with.

    The specification puts the video bytes inside a ``Motion Photo Video Data``
    box at the very end of the ISOBMFF file, so the payload is simply everything
    after the eight-byte box header.
    """
    for tail in _TAIL_STEPS:
        tail = min(tail, file_size)
        start = max(0, file_size - tail)
        data = _read(path, start, tail)
        if not data:
            continue
        index = data.find(b"mpvd")
        while index >= 0:
            box_start = index - 4
            if box_start >= 0:
                declared = struct.unpack(">I", data[box_start : box_start + 4])[0]
                absolute = start + box_start
                if declared > 8 and absolute + declared == file_size:
                    return absolute + 8, declared - 8
                if declared == 1 and box_start + 8 <= len(data):
                    large = struct.unpack(">Q", data[box_start + 8 : box_start + 16])[0]
                    if large > 16 and absolute + large == file_size:
                        return absolute + 16, large - 16
            index = data.find(b"mpvd", index + 4)
        if tail >= file_size:
            break
    return None


def _validate(path: str, start: int, length: int) -> bool:
    """The candidate range must begin with an ``ftyp`` box."""
    if start <= 0 or length <= 8:
        return False
    if start + length > _file_size(path):
        return False
    return _looks_like_mp4(_read(path, start, 16))


def detect_motion_photo(path: str) -> MotionSource | None:
    """Return where the video lives inside ``path``, or ``None``.

    Order matters: an explicit length in the XMP is authoritative and free to
    check, then the legacy offset, then the byte-level trailer scan (which is
    the only option when the metadata says "there is a video" but not how big).
    """
    if Path(path).suffix.lower() not in MOTION_STILL_EXTENSIONS:
        return None
    file_size = _file_size(path)
    if file_size < _MIN_STILL_BYTES:
        return None

    xmp = read_xmp(path)
    length = _motion_video_length(xmp) if xmp else None
    if length:
        start = file_size - length
        if _validate(path, start, length):
            return MotionSource("motion-photo", start, length, file_size=file_size)

    offset = _micro_video_offset(xmp) if xmp else None
    if offset:
        start = file_size - offset
        if _validate(path, start, file_size - start):
            return MotionSource("micro-video", start, file_size - start, file_size=file_size)

    mpvd = _find_mpvd(path, file_size)
    if mpvd and _validate(path, mpvd[0], mpvd[1]):
        return MotionSource("mpvd", mpvd[0], mpvd[1], file_size=file_size)

    # Only spend the tail scan when something suggests a video is there.
    named = bool(_MOTION_NAME.search(Path(path).stem))
    if not xmp and not named:
        return None
    trailer = _find_trailer_mp4(path, file_size)
    if trailer and _validate(path, trailer[0], trailer[1]):
        kind = "motion-photo" if _is_motion_xmp(xmp) else "trailer"
        return MotionSource(kind, trailer[0], trailer[1], file_size=file_size)
    return None


def extract_embedded(path: str, source: MotionSource, dest: str) -> bool:
    """Write the embedded video to ``dest``.

    The appended bytes are already a complete MP4, so this is a slice, not a
    conversion - no ffmpeg, no re-encode, a few milliseconds for a phone clip.
    """
    if not source.embedded:
        return False
    try:
        with open(path, "rb") as src, open(dest, "wb") as out:
            src.seek(source.start)
            remaining = source.length
            while remaining > 0:
                chunk = src.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
    except OSError:
        try:
            os.remove(dest)
        except OSError:
            pass
        return False
    return os.path.exists(dest) and os.path.getsize(dest) > 1024


def paired_video_path(still_path: str) -> str | None:
    """Find the separate video an iPhone Live Photo keeps beside the still.

    The export convention is a shared stem with a video extension
    (``IMG_1234.HEIC`` + ``IMG_1234.MOV``), which is what this looks for.
    """
    path = Path(still_path)
    if path.suffix.lower() not in MOTION_STILL_EXTENSIONS:
        return None
    directory = path.parent
    stem = path.stem
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    wanted = {ext for ext in VIDEO_EXTENSIONS}
    for name in entries:
        candidate = Path(name)
        if candidate.stem.lower() != stem.lower():
            continue
        if candidate.suffix.lower() not in wanted:
            continue
        full = os.path.join(directory, name)
        if os.path.abspath(full) == os.path.abspath(still_path):
            continue
        return full
    return None


def detect(path: str) -> MotionSource | None:
    """Every kind of live still: embedded motion photo or a paired video."""
    source = detect_motion_photo(path)
    if source:
        return source
    paired = paired_video_path(path)
    if paired:
        return MotionSource(
            "paired",
            video_path=paired,
            file_size=_file_size(path),
        )
    return None
