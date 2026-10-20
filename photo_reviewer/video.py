"""Video probing and derived assets, built on ffmpeg.

The album treats a video as one more item in the grid. Everything the rest of
the app needs from a video comes from two commands:

* ``ffprobe`` tells us the duration, the display size, the codecs and the
  bitrates - once, at index time, and cached in the database.
* ``ffmpeg`` produces the two artefacts the UI shows: a poster frame (which is
  just the ordinary thumbnail, so the whole gallery pipeline applies unchanged)
  and a short loopable clip for the detail dialog.

Nothing here knows about Flask or the database: this module takes a path and a
few numbers and returns paths and a dataclass. That keeps it testable without a
running server, and lets the tests skip cleanly on a machine without ffmpeg.

``ffprobe``/``ffmpeg`` are optional. ``find_tools`` returns ``None`` when they
are missing, and every caller degrades to "indexed, but no picture and no
playback" instead of failing the scan.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: File extensions treated as video. Covers phones (iPhone/Android), cameras
#: (MP4/MOV/MTS/M2TS from AVCHD bodies), drones (DJI MP4/MOV) and screen
#: recordings, plus the containers people end up with after editing.
VIDEO_EXTENSIONS = (
    ".mp4",
    ".m4v",
    ".mov",
    ".qt",
    ".mkv",
    ".webm",
    ".avi",
    ".wmv",
    ".flv",
    ".mpg",
    ".mpeg",
    ".m2v",
    ".dv",
    ".mts",
    ".m2ts",
    ".ts",
    ".3gp",
    ".3g2",
    ".ogv",
    ".mod",
    ".tod",
    ".insv",
    ".360",
)

#: Containers a browser opens directly, given a codec it can decode.
BROWSER_CONTAINERS = frozenset({".mp4", ".m4v", ".mov", ".qt", ".webm", ".ogv"})

#: Video codecs every Chrome/Edge/Firefox/Safari can decode.
BROWSER_CODECS = frozenset({"h264", "vp8", "vp9", "av1"})

#: Codecs that a browser may or may not decode, depending on the platform
#: (HEVC needs OS support on Windows; Safari has it everywhere).
CONDITIONAL_CODECS = frozenset({"hevc"})

#: Audio codecs that need remuxing into MP4 before a browser will play them.
BROWSER_AUDIO = frozenset({"aac", "mp3", "opus", "vorbis", "flac"})

#: How a video needs to be treated before the browser can play it.
PLAY_ORIGINAL = "original"
PLAY_REMUX = "remux"
PLAY_TRANSCODE = "transcode"


@dataclass
class Tools:
    """Paths to the two binaries, resolved once per process."""

    ffmpeg: str
    ffprobe: str


_tools_lock = threading.Lock()
_tools_cache: Tools | None = None
_tools_probed = False

#: Places ffmpeg commonly ends up on Windows when it is not on PATH.
_WINDOWS_GUESSES = (
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "ffmpeg", "bin"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"),
)
_POSIX_GUESSES = ("/usr/local/bin", "/usr/bin", "/opt/homebrew/bin")


def _binary_in(directory: str, name: str) -> str | None:
    if not directory:
        return None
    for candidate in (name, f"{name}.exe"):
        path = os.path.join(directory, candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def find_tools(override_dir: str | None = None, refresh: bool = False) -> Tools | None:
    """Locate ffmpeg and ffprobe, or return ``None`` when they are not installed.

    Order: the configured directory, then ``PATH``, then the usual install
    locations. The answer is cached because it is consulted for every video and
    every playback request.
    """
    global _tools_cache, _tools_probed
    with _tools_lock:
        if _tools_probed and not refresh:
            return _tools_cache

        directories: list[str] = []
        if override_dir:
            directories.append(os.path.expanduser(override_dir))
        ffmpeg_on_path = shutil.which("ffmpeg")
        ffprobe_on_path = shutil.which("ffprobe")
        if ffmpeg_on_path and ffprobe_on_path:
            _tools_cache = Tools(ffmpeg_on_path, ffprobe_on_path)
            _tools_probed = True
            return _tools_cache

        directories.extend(_WINDOWS_GUESSES if os.name == "nt" else _POSIX_GUESSES)
        for directory in directories:
            ffmpeg = _binary_in(directory, "ffmpeg")
            ffprobe = _binary_in(directory, "ffprobe")
            if ffmpeg and ffprobe:
                _tools_cache = Tools(ffmpeg, ffprobe)
                _tools_probed = True
                return _tools_cache

        _tools_cache = None
        _tools_probed = True
        return None


def reset_tools_cache() -> None:
    """Forget the resolved paths (settings change, tests)."""
    global _tools_cache, _tools_probed
    with _tools_lock:
        _tools_cache = None
        _tools_probed = False


def tools_available(override_dir: str | None = None) -> bool:
    return find_tools(override_dir) is not None


def tools_version(override_dir: str | None = None) -> str:
    """First line of ``ffmpeg -version``, or an empty string."""
    tools = find_tools(override_dir)
    if not tools:
        return ""
    try:
        out = subprocess.run(
            [tools.ffmpeg, "-version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=_no_window_flag(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "").splitlines()[0] if out.stdout else ""


def _no_window_flag() -> int:
    """Keep a console flash out of the way when spawning ffmpeg on Windows."""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def video_cache_key(path: str) -> str:
    """Stable cache key for a file's derived assets.

    Same rule as the photo thumbnails (path + mtime + size): editing a video in
    place changes its mtime, which produces a new key, which is what lets the
    cached poster and preview expire without tracking versions anywhere.
    """
    try:
        stat = os.stat(path)
        key = f"{path}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        key = path
    return hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:20]


def _run(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=_no_window_flag(),
    )


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

#: ``ffmpeg`` writes "2024-05-01T10:20:30.000000Z" or "2024:05:01 10:20:30".
_ISO_TAG = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")
_EXIF_TAG = re.compile(r"^(\d{4})[:/-](\d{2})[:/-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def normalize_capture_time(value: Any) -> str | None:
    """Turn a container timestamp into the ``YYYY:MM:DD HH:MM:SS`` EXIF shape.

    Photos store their capture time as ``2024:05:01 10:20:30`` in the EXIF
    block, and every consumer downstream (year filter, time sort, the detail
    row) reads that one shape. Videos carry the same instant as ISO-8601 in
    ``creation_time``; matching the shape is what lets a video sort next to the
    photos taken in the same minute.
    """
    text = str(value or "").strip()
    if not text:
        return None
    match = _ISO_TAG.match(text)
    if not match:
        match = _EXIF_TAG.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    # A zeroed timestamp (some encoders write 1970-01-01 or 0000:00:00) is not
    # a capture time; showing "1970年1月1日" would be worse than showing nothing.
    if year in ("0000", "1970") or month == "00" or day == "00":
        return None
    return f"{year}:{month}:{day} {hour}:{minute}:{second}"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


_DECIMAL_LATITUDE = re.compile(r"^([+-]?\d{1,3}(?:\.\d+)?)$")


def parse_iso6709(value: Any) -> tuple[float, float] | None:
    """Read the ``+30.2500+120.1700/`` location tag ffmpeg writes.

    Phone videos carry GPS as ISO 6709 rather than EXIF rationals, so a video
    needs its own little parser to end up in the same ``latitude``/``longitude``
    fields the photo path fills.
    """
    text = str(value or "").strip()
    if not text:
        return None
    parts = re.findall(r"[+-]\d+(?:\.\d+)?", text)
    if len(parts) < 2:
        return None
    try:
        latitude = float(parts[0])
        longitude = float(parts[1])
    except ValueError:
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    return latitude, longitude


@dataclass
class VideoInfo:
    """What the app knows about one video file."""

    duration_ms: int = 0
    width: int = 0
    height: int = 0
    rotation: int = 0
    fps: float = 0.0
    video_codec: str = ""
    video_profile: str = ""
    pix_fmt: str = ""
    video_bitrate: int = 0
    audio_codec: str = ""
    audio_bitrate: int = 0
    audio_channels: int = 0
    container: str = ""
    capture_time: str = ""
    make: str = ""
    model: str = ""
    latitude: float | None = None
    longitude: float | None = None
    has_audio: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration_ms": self.duration_ms,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "fps": self.fps,
            "video_codec": self.video_codec,
            "video_profile": self.video_profile,
            "pix_fmt": self.pix_fmt,
            "video_bitrate": self.video_bitrate,
            "audio_codec": self.audio_codec,
            "audio_bitrate": self.audio_bitrate,
            "audio_channels": self.audio_channels,
            "container": self.container,
            "capture_time": self.capture_time,
            "make": self.make,
            "model": self.model,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "has_audio": self.has_audio,
        }

    @property
    def play_mode(self) -> str:
        """How this file has to be prepared before a browser can play it."""
        codec = self.video_codec.lower()
        container = self.container.lower()
        browser_container = not container or container in BROWSER_CONTAINERS
        browser_codec = codec in BROWSER_CODECS or codec in CONDITIONAL_CODECS
        if browser_container and browser_codec:
            return PLAY_ORIGINAL
        if codec in BROWSER_CODECS or codec in CONDITIONAL_CODECS:
            # Same bits, different wrapper: a copy remux is enough.
            return PLAY_REMUX
        return PLAY_TRANSCODE

    @property
    def needs_transcode(self) -> bool:
        return self.play_mode == PLAY_TRANSCODE


def _container_from_format(format_name: str) -> str:
    """Best-effort container guess from ffprobe's ``format_name`` alone."""
    names = {part.strip() for part in str(format_name or "").split(",") if part.strip()}
    if names & {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}:
        return ".mp4"
    if "webm" in names:
        return ".webm"
    if "matroska" in names:
        return ".mkv"
    if "mpegts" in names:
        return ".ts"
    if "avi" in names:
        return ".avi"
    if "mpeg" in names:
        return ".mpg"
    return ""


def container_for(path: str, format_name: str = "") -> str:
    """The container a browser has to open, as an extension.

    The file's own extension is the honest answer for ``play_mode``: ffprobe
    reports both Matroska and WebM as ``matroska,webm``, yet a browser opens the
    second and refuses the first. The probe's format string only decides when
    the name has no useful extension.
    """
    suffix = os.path.splitext(path)[1].lower()
    if suffix:
        return suffix
    return _container_from_format(format_name)


def parse_probe(data: dict[str, Any]) -> VideoInfo:
    """Turn ``ffprobe -show_format -show_streams`` JSON into a ``VideoInfo``.

    Kept separate from the subprocess call so it can be tested against captured
    JSON on a machine without ffmpeg.
    """
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), {}) or {}
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {}) or {}

    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(float(side["rotation"])) % 360
            except (TypeError, ValueError):
                rotation = 0
            break
    # Older muxers put the turn in the stream tag instead of the display matrix.
    if not rotation:
        try:
            rotation = int(float(video.get("tags", {}).get("rotate", 0))) % 360
        except (TypeError, ValueError):
            rotation = 0

    width = _int(video.get("width"))
    height = _int(video.get("height"))
    if rotation in (90, 270) and width and height:
        # Report what the viewer sees, exactly like the EXIF orientation rule
        # for stills: a portrait clip must not claim to be landscape.
        width, height = height, width

    duration_ms = _int(float(_float(fmt.get("duration"), 0.0)) * 1000)
    if not duration_ms and video.get("duration"):
        duration_ms = _int(_float(video.get("duration"), 0.0) * 1000)

    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = str(video.get(key) or "")
        if "/" in rate:
            try:
                num, den = rate.split("/", 1)
                num_f, den_f = float(num), float(den)
                if den_f:
                    fps = num_f / den_f
            except (TypeError, ValueError):
                fps = 0.0
        elif rate:
            fps = _float(rate)
        if fps > 0:
            break

    tags = {}
    for source in (fmt.get("tags") or {}, video.get("tags") or {}):
        for key, value in source.items():
            tags.setdefault(str(key).lower(), value)

    def tag(*names: str) -> str:
        for name in names:
            value = tags.get(name)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    coordinates = parse_iso6709(tag("location", "com.apple.quicktime.location.iso6709"))
    format_name = str(fmt.get("format_name") or "")
    container = _container_from_format(format_name)

    info = VideoInfo(
        duration_ms=duration_ms,
        width=width,
        height=height,
        rotation=rotation,
        fps=round(fps, 3),
        video_codec=str(video.get("codec_name") or ""),
        video_profile=str(video.get("profile") or ""),
        pix_fmt=str(video.get("pix_fmt") or ""),
        video_bitrate=_int(video.get("bit_rate")) or _int(fmt.get("bit_rate")),
        audio_codec=str(audio.get("codec_name") or ""),
        audio_bitrate=_int(audio.get("bit_rate")),
        audio_channels=_int(audio.get("channels")),
        container=container,
        capture_time=normalize_capture_time(
            tag("creation_time", "com.apple.quicktime.creationdate", "date")
        )
        or "",
        make=tag("make", "com.apple.quicktime.make"),
        model=tag("model", "com.apple.quicktime.model", "com.android.manufacturer"),
        latitude=coordinates[0] if coordinates else None,
        longitude=coordinates[1] if coordinates else None,
        has_audio=bool(audio),
    )
    info.extra = {"format_name": format_name}
    return info


def probe(path: str, override_dir: str | None = None) -> VideoInfo | None:
    """Run ffprobe on ``path`` and parse the result. ``None`` when unavailable."""
    tools = find_tools(override_dir)
    if not tools:
        return None
    try:
        out = _run(
            [
                tools.ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc)
        return None
    if out.returncode != 0:
        logger.warning("ffprobe rejected %s: %s", path, (out.stderr or "").strip()[:300])
        return None
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        return None
    if not data.get("streams"):
        return None
    info = parse_probe(data)
    # The file's own extension is the container the browser must open, and it
    # is the only way to tell WebM (playable) from Matroska (not) - ffprobe
    # reports both as the same format name.
    info.container = container_for(path, (data.get("format") or {}).get("format_name"))
    return info


# ---------------------------------------------------------------------------
# Derived files
# ---------------------------------------------------------------------------


def poster_args(
    source: str,
    dest: str,
    seek: float,
    max_edge: int,
    quality: int,
) -> list[str]:
    """ffmpeg arguments for one still frame, scaled and rotated upright.

    ``-ss`` before ``-i`` is the fast seek: ffmpeg jumps instead of decoding
    from the start, which is what keeps a 4K clip from costing seconds.
    Rotation is applied automatically (that is ffmpeg's default), matching what
    every player shows and what ``VideoInfo`` reports.
    """
    return [
        "-v",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, seek):.3f}",
        "-i",
        source,
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({max_edge},iw)':-2",
        "-q:v",
        str(max(1, min(31, quality))),
        dest,
    ]


def clip_args(
    source: str,
    dest: str,
    start: float,
    seconds: float,
    max_edge: int,
    crf: int,
    include_audio: bool,
) -> list[str]:
    """ffmpeg arguments for the short loopable preview.

    H.264 + ``yuv420p`` is the one combination every desktop browser decodes
    without a plugin; ``+faststart`` moves the index to the front so playback
    can begin before the file is fully read. Audio is included but downmixed
    and small, because the element plays muted and the speaker button is an
    opt-in.
    """
    args = [
        "-v",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-i",
        source,
        "-t",
        f"{max(0.5, seconds):.3f}",
        "-vf",
        f"scale='min({max_edge},iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(max(0, min(51, crf))),
        "-pix_fmt",
        "yuv420p",
        # A stream copied from Matroska or MPEG-TS often starts at a negative
        # timestamp (the AAC priming samples). MP4 keeps that offset, and Chrome
        # then refuses the whole file with "Failed to send audio packet".
        # Shifting the timeline to zero is what makes the clip play everywhere.
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
    ]
    if include_audio:
        args += ["-c:a", "aac", "-b:a", "64k", "-ac", "1"]
    else:
        args += ["-an"]
    args.append(dest)
    return args


def remux_args(source: str, dest: str, codec: str = "") -> list[str]:
    """ffmpeg arguments for a stream copy into MP4.

    No re-encode: the frames are copied, so this costs milliseconds and loses
    nothing. ``hvc1`` is the tag Apple devices and some Chrome builds require;
    HEVC files muxed as ``hev1`` play back as a black screen without it.
    """
    args = [
        "-v",
        "error",
        "-y",
        "-i",
        source,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
    ]
    if str(codec).lower() == "hevc":
        args += ["-tag:v", "hvc1"]
    args.append(dest)
    return args


def transcode_args(source: str, dest: str, max_edge: int, crf: int) -> list[str]:
    """ffmpeg arguments for the last-resort H.264 conversion.

    Only used when the codec itself is unplayable (ProRes, MPEG-2, MJPEG...).
    """
    return [
        "-v",
        "error",
        "-y",
        "-i",
        source,
        "-vf",
        f"scale='min({max_edge},iw)':-2:flags=bicubic",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(max(0, min(51, crf))),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        dest,
    ]


def poster_seek(info: VideoInfo | None, ratio: float = 0.10, minimum: float = 1.5) -> float:
    """Where to grab the poster frame.

    A drone clip often opens on a dark, half-moved frame; ten percent in gives
    a representative one. Very short clips would run past the end, so they start
    a fixed distance in and fall back to zero when even that is too far.
    """
    duration = (info.duration_ms if info else 0) / 1000.0
    if duration <= 0:
        return 0.0
    if duration <= minimum * 2:
        return 0.0
    return min(duration * ratio, max(minimum, duration * 0.5))


def clip_window(info: VideoInfo | None, seconds: float) -> tuple[float, float]:
    """``(start, duration)`` for the preview clip.

    Starts a little way in - the first frames of a long clip are usually the
    least interesting - and never runs past the end of the file.
    """
    duration = (info.duration_ms if info else 0) / 1000.0
    if duration <= 0:
        return 0.0, seconds
    length = min(seconds, duration)
    start = 0.0
    if duration > length + 1.0:
        start = min(duration * 0.10, duration - length)
    return max(0.0, start), length


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _temp_target(dest: str) -> str:
    """A scratch name that keeps ``dest``'s extension.

    ffmpeg picks its muxer from the file name, so a plain ``.tmp`` suffix makes
    it fail with "unable to choose an output format". Keeping the real extension
    at the end also means a crash leaves an obviously temporary file behind
    rather than something that looks like a real asset.
    """
    stem, ext = os.path.splitext(dest)
    return f"{stem}.{os.getpid()}.tmp{ext}"


def _run_ffmpeg(tools: Tools, args: list[str], timeout: float) -> bool:
    try:
        out = _run([tools.ffmpeg, *args], timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg failed: %s", exc)
        return False
    if out.returncode != 0:
        logger.warning("ffmpeg rejected the job: %s", (out.stderr or "").strip()[:300])
        return False
    return True


def make_poster(
    source: str,
    dest: str,
    seek: float,
    max_edge: int = 640,
    quality: int = 4,
    override_dir: str | None = None,
) -> bool:
    """Write one JPEG frame from ``source``. Returns whether it worked."""
    tools = find_tools(override_dir)
    if not tools:
        return False
    _ensure_parent(dest)
    temp = _temp_target(dest)
    if _run_ffmpeg(tools, poster_args(source, temp, seek, max_edge, quality), 120):
        try:
            os.replace(temp, dest)
        except OSError:
            _discard(temp)
            return False
        return os.path.exists(dest) and os.path.getsize(dest) > 0
    _discard(temp)
    return False


def make_clip(
    source: str,
    dest: str,
    info: VideoInfo | None,
    seconds: float = 5.0,
    max_edge: int = 960,
    crf: int = 28,
    include_audio: bool = True,
    override_dir: str | None = None,
) -> bool:
    """Write the short preview clip. Returns whether it worked."""
    tools = find_tools(override_dir)
    if not tools:
        return False
    _ensure_parent(dest)
    start, length = clip_window(info, seconds)
    temp = _temp_target(dest)
    args = clip_args(source, temp, start, length, max_edge, crf, include_audio)
    if _run_ffmpeg(tools, args, 600):
        try:
            os.replace(temp, dest)
        except OSError:
            _discard(temp)
            return False
        return os.path.exists(dest) and os.path.getsize(dest) > 0
    _discard(temp)
    return False


def make_playable(
    source: str,
    dest: str,
    info: VideoInfo | None,
    max_edge: int = 1280,
    crf: int = 23,
    override_dir: str | None = None,
) -> bool:
    """Produce a browser-playable MP4: copy when possible, convert otherwise."""
    tools = find_tools(override_dir)
    if not tools:
        return False
    _ensure_parent(dest)
    codec = (info.video_codec if info else "") or ""
    mode = info.play_mode if info else PLAY_REMUX
    _discard(dest)

    temp = _temp_target(dest)
    if mode != PLAY_TRANSCODE and _run_ffmpeg(
        tools, remux_args(source, temp, codec), 900
    ):
        try:
            os.replace(temp, dest)
        except OSError:
            _discard(temp)
        else:
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                return True
    _discard(temp)
    temp = _temp_target(dest)
    if _run_ffmpeg(tools, transcode_args(source, temp, max_edge, crf), 3600):
        try:
            os.replace(temp, dest)
        except OSError:
            _discard(temp)
            return False
        return os.path.exists(dest) and os.path.getsize(dest) > 0
    _discard(temp)
    return False


def _discard(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def looks_like_video(path: str) -> bool:
    from pathlib import Path

    return Path(path).suffix.lower() in VIDEO_EXTENSIONS
