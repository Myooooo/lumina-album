"""Configuration for the local photo review system.

Settings can be provided through environment variables, and are persisted to
``<data_dir>/settings.json`` when changed from the web UI.  On the next start
the saved settings are loaded automatically, so no configuration is lost.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


PERSISTED_FIELDS = (
    "api_base_url",
    "api_key",
    "model",
    "system_prompt",
    "proxy_max_edge",
    "proxy_quality",
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
    "host",
    "port",
    "debug",
)


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

    # Image proxy generation.
    proxy_max_edge: int = int(os.getenv("PHOTO_PROXY_MAX_EDGE", "1024"))
    proxy_quality: int = int(os.getenv("PHOTO_PROXY_QUALITY", "85"))

    # Scanning.
    scan_concurrency: int = int(os.getenv("PHOTO_SCAN_CONCURRENCY", "1"))
    index_concurrency: int = int(os.getenv("PHOTO_INDEX_CONCURRENCY", "4"))
    request_timeout: int = int(os.getenv("PHOTO_REQUEST_TIMEOUT", "120"))
    model_retries: int = int(os.getenv("PHOTO_MODEL_RETRIES", "2"))
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
    geocoding_interval: float = float(os.getenv("PHOTO_GEOCODING_INTERVAL", "1.0"))
    geocoding_retries: int = int(os.getenv("PHOTO_GEOCODING_RETRIES", "3"))
    host: str = field(default_factory=lambda: os.getenv("PHOTO_HOST", "127.0.0.1"))
    port: int = int(os.getenv("PHOTO_PORT", "5000"))
    debug: bool = _env_bool("PHOTO_DEBUG", False)

    # Allowed image extensions.
    image_extensions: tuple = (
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".tif",
        ".tiff",
    )

    # Runtime-computed settings path.
    settings_path: str = ""

    def __post_init__(self) -> None:
        data = Path(self.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        if not self.db_path:
            self.db_path = str(data / "library.db")
        self.settings_path = str(data / "settings.json")
        self._load()

    def _load(self) -> None:
        if not self.settings_path or not os.path.exists(self.settings_path):
            return
        try:
            with open(self.settings_path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        for key in PERSISTED_FIELDS:
            if key in saved and hasattr(self, key):
                value = saved[key]
                if key in {
                    "proxy_max_edge",
                    "proxy_quality",
                    "scan_concurrency",
                    "index_concurrency",
                    "request_timeout",
                    "model_retries",
                    "geocoding_retries",
                    "port",
                }:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                elif key == "geocoding_interval":
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                elif key == "debug":
                    value = bool(value)
                setattr(self, key, value)

    def save(self) -> None:
        data = {key: getattr(self, key) for key in PERSISTED_FIELDS}
        Path(self.settings_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.settings_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_base_url": self.api_base_url,
            "api_key": self.api_key,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "proxy_max_edge": self.proxy_max_edge,
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
        }


# A process-wide mutable config object. The Flask app updates this when the
# user changes settings in the UI.
CONFIG = Config()
