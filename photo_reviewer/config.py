"""Configuration for the local photo review system.

Settings can be provided through environment variables, and the ones the user
can change in the web UI are persisted to the SQLite database (``settings``
table inside ``<data_dir>/library.db``). On the next start those values are
loaded automatically, so no configuration is lost.

``host``/``port``/``debug`` are deliberately excluded from that round trip:
they govern how the process boots and stay under the operator's control.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .thumbnailer import HEIF_AVAILABLE, HEIF_EXTENSIONS


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Fields written to and restored from the SQLite ``settings`` table.
#
# ``host``/``port``/``debug`` are intentionally NOT listed: they decide how the
# process binds and boots, so they must stay under the operator's control
# through the environment instead of being pinned by a row written during an
# earlier run (a persisted ``debug=true`` would re-enable the Werkzeug
# interactive debugger on an unrelated later start).
PERSISTED_FIELDS = (
    "api_base_url",
    "api_key",
    "model",
    "system_prompt",
    "proxy_max_edge",
    "gallery_thumb_size",
    "proxy_quality",
    "thumb_quality",
    "scan_concurrency",
    "index_concurrency",
    "request_timeout",
    "model_retries",
    "trash_dir_name",
    "cache_dir_name",
    "geocoding_provider",
    "geocoding_api_key",
    "geocoding_interval",
    "geocoding_retries",
    "video_clip_seconds",
    "video_clip_max_edge",
    "video_clip_crf",
    "video_clip_audio",
    "video_pregenerate",
    "video_poster_ratio",
    "video_processing",
    "video_preview_volume",
    "video_preview_mode",
    "live_volume",
    "merge_live_photos",
)

# Fields the web UI is allowed to change through ``POST /api/config``. The UI
# must never be able to move the cache/trash directories or rebind the server.
EDITABLE_FIELDS = frozenset(PERSISTED_FIELDS) - {"trash_dir_name", "cache_dir_name"}

#: How the scan treats video files.
#: ``ffmpeg`` indexes them; ``none`` walks past them entirely.
VIDEO_PROCESSING = ("ffmpeg", "none")

#: What the detail-page preview does when a video is opened. ``off`` keeps the
#: poster, ``once`` plays the clip and leaves the last frame, ``loop`` repeats.
PREVIEW_MODES = ("off", "once", "loop")


@dataclass
class Config:
    # Local model endpoint that speaks the OpenAI Chat Completions protocol.
    api_base_url: str = field(
        default_factory=lambda: os.getenv(
            "PHOTO_API_BASE_URL", "http://localhost:1234/v1"
        )
    )
    api_key: str = field(
        default_factory=lambda: os.getenv("PHOTO_API_KEY", "not-needed")
    )
    model: str = field(default_factory=lambda: os.getenv("PHOTO_MODEL", "local-model"))

    # Custom system prompt. Empty means use the built-in default prompt.
    system_prompt: str = field(
        default_factory=lambda: os.getenv("PHOTO_SYSTEM_PROMPT", "")
    )

    # Image proxy generation. Every numeric field below uses ``default_factory``
    # so the environment is read once per instance instead of once at import
    # time; a plain ``int(os.getenv(...))`` default is frozen when the module is
    # first imported and silently ignores later environment changes.
    proxy_max_edge: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_PROXY_MAX_EDGE", "1280"))
    )
    gallery_thumb_size: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_GALLERY_THUMB_SIZE", "640"))
    )
    proxy_quality: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_PROXY_QUALITY", "80"))
    )
    thumb_quality: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_THUMB_QUALITY", "50"))
    )

    # Scanning.
    scan_concurrency: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_SCAN_CONCURRENCY", "1"))
    )
    index_concurrency: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_INDEX_CONCURRENCY", "4"))
    )
    request_timeout: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_REQUEST_TIMEOUT", "120"))
    )
    model_retries: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_MODEL_RETRIES", "3"))
    )
    # Storage.
    data_dir: str = field(
        default_factory=lambda: os.getenv(
            "PHOTO_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")
        )
    )
    db_path: str = field(default_factory=lambda: os.getenv("PHOTO_DB_PATH", ""))
    trash_dir_name: str = field(
        default_factory=lambda: os.getenv("PHOTO_TRASH_DIR_NAME", ".photo-trash")
    )
    cache_dir_name: str = field(
        default_factory=lambda: os.getenv("PHOTO_CACHE_DIR_NAME", ".photo-review-cache")
    )
    geocoding_provider: str = field(
        default_factory=lambda: os.getenv("PHOTO_GEOCODING_PROVIDER", "nominatim")
    )
    geocoding_api_key: str = field(
        default_factory=lambda: os.getenv("PHOTO_GEOCODING_API_KEY", "")
    )
    geocoding_interval: float = field(
        default_factory=lambda: float(os.getenv("PHOTO_GEOCODING_INTERVAL", "1.0"))
    )
    geocoding_retries: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_GEOCODING_RETRIES", "3"))
    )
    host: str = field(default_factory=lambda: os.getenv("PHOTO_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("PHOTO_PORT", "5000")))
    debug: bool = field(default_factory=lambda: _env_bool("PHOTO_DEBUG", False))

    # Extra origins allowed to call mutating endpoints, for reverse proxies or
    # a second hostname. Comma separated; the app's own host is always allowed.
    allowed_origins: tuple = field(
        default_factory=lambda: tuple(
            item.strip()
            for item in os.getenv("PHOTO_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        )
    )

    # Allowed image extensions (processed directly). HEIC/AVIF join the list
    # only when ``pillow-heif`` is installed: promising to index a format the
    # process cannot decode would turn every such file into an error row.
    image_extensions: tuple = field(
        default_factory=lambda: (
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".bmp",
            ".webp",
            ".tif",
            ".tiff",
        )
        + (HEIF_EXTENSIONS if HEIF_AVAILABLE else ())
    )

    # Camera raw formats. These are only indexed when no same-named
    # ``image_extensions`` file sits next to them, and they are rendered from
    # their embedded JPEG preview instead of the sensor data.
    raw_extensions: tuple = (
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
    )

    # Video files. Indexed alongside the photos and rendered through ffmpeg;
    # the extension list covers phones (MP4/MOV), cameras (AVCHD's MTS/M2TS),
    # drones and the containers an edit usually leaves behind.
    video_extensions: tuple = (
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

    # Video previews. The detail dialog loops a short clip; the numbers below
    # decide how long it is and how hard it is compressed. They are editable
    # from the settings dialog because the right answer depends on the machine.
    video_clip_seconds: float = field(
        default_factory=lambda: float(os.getenv("PHOTO_VIDEO_CLIP_SECONDS", "3"))
    )
    video_clip_max_edge: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_VIDEO_CLIP_MAX_EDGE", "960"))
    )
    video_clip_crf: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_VIDEO_CLIP_CRF", "28"))
    )
    video_clip_audio: bool = field(
        default_factory=lambda: _env_bool("PHOTO_VIDEO_CLIP_AUDIO", True)
    )
    #: Generate the preview during indexing instead of on first open. Costs
    #: ~200 ms per video up front, and makes the first click instant.
    video_pregenerate: bool = field(
        default_factory=lambda: _env_bool("PHOTO_VIDEO_PREGENERATE", False)
    )
    #: Show a Live Photo as one item (photo with its motion attached) or as two
    #: (the still and the movie side by side). Read at scan time, so changing it
    #: takes effect on the next "重新整理".
    merge_live_photos: bool = field(
        default_factory=lambda: _env_bool("PHOTO_MERGE_LIVE_PHOTOS", True)
    )
    #: Where the poster frame is taken from, as a fraction of the duration.
    video_poster_ratio: float = field(
        default_factory=lambda: float(os.getenv("PHOTO_VIDEO_POSTER_RATIO", "0.1"))
    )
    #: Playback volume of the detail-page preview, 0-100. Independent of the
    #: preview *mode*: a silent loop is usually what people want, so the default
    #: is half volume rather than full.
    video_preview_volume: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_VIDEO_PREVIEW_VOLUME", "50"))
    )
    #: What the preview does when the dialog opens: ``off``, ``once`` or
    #: ``loop``. "once" leaves the poster on screen when the clip ends, which
    #: reads as a glance rather than a video player.
    video_preview_mode: str = field(
        default_factory=lambda: os.getenv("PHOTO_VIDEO_PREVIEW_MODE", "once")
    )
    #: Volume of a Live Photo's motion, 0-100. Silent by default: the motion is
    #: ambience, and a photo that suddenly talks is startling.
    live_volume: int = field(
        default_factory=lambda: int(os.getenv("PHOTO_LIVE_VOLUME", "0"))
    )
    #: How videos are handled on a scan: ``ffmpeg`` indexes them (probe, poster,
    #: preview), ``none`` skips them entirely. Read when the folder is walked, so
    #: turning it off only affects later scans - rows already in the database
    #: keep working, and their files stay untouched either way.
    video_processing: str = field(
        default_factory=lambda: os.getenv("PHOTO_VIDEO_PROCESSING", "ffmpeg")
    )
    #: Directory holding ffmpeg/ffprobe. Environment only: it describes the
    #: machine, not the album, so it must not be pinned by a database row.
    ffmpeg_path: str = field(default_factory=lambda: os.getenv("PHOTO_FFMPEG_PATH", ""))

    def __post_init__(self) -> None:
        data = Path(self.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        if not self.db_path:
            self.db_path = str(data / "library.db")

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_base_url": self.api_base_url,
            "api_key": self.api_key,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "proxy_max_edge": self.proxy_max_edge,
            "gallery_thumb_size": self.gallery_thumb_size,
            "proxy_quality": self.proxy_quality,
            "thumb_quality": self.thumb_quality,
            "scan_concurrency": self.scan_concurrency,
            "index_concurrency": self.index_concurrency,
            "request_timeout": self.request_timeout,
            "model_retries": self.model_retries,
            "data_dir": self.data_dir,
            "trash_dir_name": self.trash_dir_name,
            "cache_dir_name": self.cache_dir_name,
            "geocoding_provider": self.geocoding_provider,
            "geocoding_api_key": self.geocoding_api_key,
            "geocoding_interval": self.geocoding_interval,
            "geocoding_retries": self.geocoding_retries,
            "image_extensions": list(self.image_extensions),
            "raw_extensions": list(self.raw_extensions),
            "video_extensions": list(self.video_extensions),
            "video_clip_seconds": self.video_clip_seconds,
            "video_clip_max_edge": self.video_clip_max_edge,
            "video_clip_crf": self.video_clip_crf,
            "video_clip_audio": self.video_clip_audio,
            "video_pregenerate": self.video_pregenerate,
            "video_poster_ratio": self.video_poster_ratio,
            "video_processing": self.video_processing,
            "video_preview_volume": self.video_preview_volume,
            "video_preview_mode": self.video_preview_mode,
            "live_volume": self.live_volume,
            "merge_live_photos": self.merge_live_photos,
        }


# A process-wide mutable config object. The Flask app updates this when the
# user changes settings in the UI.
CONFIG = Config()
