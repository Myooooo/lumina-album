"""Reverse geocoding helpers for turning GPS coordinates into place names."""

from __future__ import annotations

import math
import threading
import time
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Coordinate Transformation (WGS-84 to GCJ-02 & GCJ-02 to WGS-84)
# ---------------------------------------------------------------------------

_EE = 0.00669342162296594323
_A = 6378245.0


def _out_of_china(lat: float, lon: float) -> bool:
    if lon < 72.004 or lon > 137.8347:
        return True
    if lat < 0.8293 or lat > 55.8271:
        return True
    return False


def _transform_lat(x: float, y: float) -> float:
    ret = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    ret += (
        (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi))
        * 2.0
        / 3.0
    )
    ret += (
        (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi))
        * 2.0
        / 3.0
    )
    ret += (
        (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0))
        * 2.0
        / 3.0
    )
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
    )
    ret += (
        (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi))
        * 2.0
        / 3.0
    )
    ret += (
        (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi))
        * 2.0
        / 3.0
    )
    ret += (
        (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi))
        * 2.0
        / 3.0
    )
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    """Convert standard GPS (WGS-84) coordinates to China National standard (GCJ-02)."""
    if _out_of_china(lat, lon):
        return lat, lon
    d_lat = _transform_lat(lon - 105.0, lat - 35.0)
    d_lon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    d_lon = (d_lon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lat + d_lat, lon + d_lon


def gcj02_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    """Convert GCJ-02 coordinates to WGS-84 coordinates."""
    if _out_of_china(lat, lon):
        return lat, lon
    g_lat, g_lon = wgs84_to_gcj02(lat, lon)
    d_lat = g_lat - lat
    d_lon = g_lon - lon
    return lat - d_lat, lon - d_lon


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


def _amap_location(lat: float, lon: float) -> str:
    """高德逆地理编码要求：GCJ-02坐标系，经度在前，纬度在后。
    
    相机/手机拍摄原始EXIF记录的是WGS-84标准坐标，调用高德前自动转换为GCJ-02火星坐标。
    """
    g_lat, g_lon = wgs84_to_gcj02(lat, lon)
    return f"{g_lon:.6f},{g_lat:.6f}"


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
                "location": _amap_location(lat, lon),
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
