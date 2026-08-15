"""Reverse geocoding helpers for turning GPS coordinates into place names."""

from __future__ import annotations

import threading
import time
from typing import Any

import requests


def _get_json(
    url: str,
    params: dict,
    headers: dict | None = None,
    retries: int = 3,
    timeout: int = 8,
) -> dict[str, Any] | None:
    """GET JSON from a geocoding provider with exponential retries.

    The first retry happens after 1 second, then the delay doubles.
    """
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException:
            if attempt >= retries:
                return None
            time.sleep(1.0 * (2**attempt))
            continue
        if resp.status_code != 200:
            if attempt >= retries:
                return None
            time.sleep(1.0 * (2**attempt))
            continue
        try:
            return resp.json()
        except ValueError:
            return None
    return None


_GEOCODE_LOCK = threading.Lock()
_LAST_GEOCODE_REQUEST = 0.0


def _wait_for_queue_slot(interval: float) -> None:
    """Serialize geocoding requests and enforce the configured minimum interval."""
    global _LAST_GEOCODE_REQUEST
    interval = max(0.1, float(interval or 1.0))
    with _GEOCODE_LOCK:
        now = time.monotonic()
        remaining = interval - (now - _LAST_GEOCODE_REQUEST)
        if remaining > 0:
            time.sleep(remaining)
        _LAST_GEOCODE_REQUEST = time.monotonic()


def reverse_geocode(
    lat: float,
    lon: float,
    provider: str = "nominatim",
    api_key: str = "",
    interval: float = 1.0,
    retries: int = 3,
) -> str | None:
    """Convert coordinates to a human-readable Chinese place name.

    Defaults to Nominatim (free, no API key). If ``provider`` is ``amap`` and
    an API key is provided, uses AMap (高德) reverse geocoding instead.
    All requests pass through a serialized queue with ``interval`` seconds
    between two requests.
    """
    _wait_for_queue_slot(interval)
    if provider == "amap" and api_key:
        data = _get_json(
            "https://restapi.amap.com/v3/geocode/regeo",
            {
                "location": f"{lon:.6f},{lat:.6f}",
                "key": api_key,
                "extensions": "base",
                "output": "json",
            },
            retries=retries,
        )
        if data and str(data.get("status")) == "1":
            regeocode = data.get("regeocode") or {}
            formatted = regeocode.get("formatted_address")
            if formatted:
                return str(formatted).strip()

        # AMap returned nothing useful; fall back to the free provider.
        _wait_for_queue_slot(interval)

    data = _get_json(
        "https://nominatim.openstreetmap.org/reverse",
        {
            "format": "jsonv2",
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
            "accept-language": "zh-CN",
            "zoom": 16,
        },
        headers={"User-Agent": "LocalPhotoAlbum/1.0 (personal use)"},
        retries=retries,
    )
    return (data.get("display_name") or data.get("name") or None) if data else None
