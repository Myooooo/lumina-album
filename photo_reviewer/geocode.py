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

#: Provider value that turns geocoding off entirely: coordinates are shown as
#: they are and nothing is ever sent over the network.
PROVIDER_NONE = "none"

#: Every provider the settings dialog may store.
PROVIDERS = ("nominatim", "amap", PROVIDER_NONE)


def geocoding_enabled(provider: str | None) -> bool:
    """False when the user asked for no reverse geocoding at all."""
    return str(provider or "").strip().lower() != PROVIDER_NONE


def _out_of_china(lat: float, lon: float) -> bool:
    return lon < 72.004 or lon > 137.8347 or lat < 0.8293 or lat > 55.8271


def _transform_lat(x: float, y: float) -> float:
    ret = (
        -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    )
    ret += (
        (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi))
        * 2.0
        / 3.0
    )
    ret += (
        (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    )
    ret += (
        (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0))
        * 2.0
        / 3.0
    )
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (
        (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi))
        * 2.0
        / 3.0
    )
    ret += (
        (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
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
    """Convert China GCJ-02 coordinates back to standard WGS-84."""
    if _out_of_china(lat, lon):
        return lat, lon
    g_lat, g_lon = wgs84_to_gcj02(lat, lon)
    return lat * 2.0 - g_lat, lon * 2.0 - g_lon


def _parse_amap_lonlat(raw: str) -> tuple[float, float] | None:
    """Parse AMap 'lon,lat' (GCJ-02) into standard WGS-84 (lat, lon)."""
    text = str(raw or "").strip()
    if "," not in text:
        return None
    try:
        lon_s, lat_s = text.split(",", 1)
        g_lon = float(lon_s.strip())
        g_lat = float(lat_s.strip())
    except ValueError:
        return None
    return gcj02_to_wgs84(g_lat, g_lon)


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
    allow_fallback: bool = False,
) -> str | None:
    """Convert coordinates to a human-readable Chinese place name.

    Defaults to Nominatim (free, no API key). If ``provider`` is ``amap`` and
    an API key is provided, uses AMap (高德) reverse geocoding instead.
    All requests pass through a serialized queue with ``interval`` seconds
    between two requests.

    ``allow_fallback`` decides what happens when 高德 answers without an
    address. Off by default, because falling back to OSM sends the coordinates
    to a *different* company than the one the user configured - a network
    destination they never chose. With it off the coordinates simply stay
    unresolved and the caller keeps them as they are.

    Returns ``None`` straight away when the provider is ``none``: the caller
    then keeps the raw coordinates, which is what "不使用" means to the user.
    """
    if not geocoding_enabled(provider):
        return None
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

        if not allow_fallback:
            # 高德 was the chosen provider and it had no answer. Sending the
            # same coordinates to OSM anyway would be a second, unchosen
            # recipient for the user's location.
            return None
        # The user explicitly allowed the free provider as a backup.
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


def forward_geocode(
    query: str,
    provider: str = "nominatim",
    api_key: str = "",
    interval: float = 1.0,
    retries: int = 3,
    allow_fallback: bool = False,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Resolve a human-entered place name into WGS-84 coordinate candidates."""
    text = str(query or "").strip()
    if not text or not geocoding_enabled(provider):
        return []
    limit = max(1, min(int(limit or 5), 10))
    candidates: list[dict[str, Any]] = []

    if provider == "amap" and api_key:
        _wait_for_queue_slot(interval)
        data = _get_json(
            "https://restapi.amap.com/v3/geocode/geo",
            {
                "address": text,
                "key": api_key,
                "output": "json",
            },
            retries=retries,
        )
        if data and str(data.get("status")) == "1":
            for item in (data.get("geocodes") or [])[:limit]:
                coords = _parse_amap_lonlat(item.get("location", ""))
                if not coords:
                    continue
                city_val = item.get("city")
                if isinstance(city_val, list):
                    city_val = ""
                province_val = item.get("province") or ""
                if isinstance(province_val, list):
                    province_val = ""
                formatted = str(item.get("formatted_address") or text).strip()
                candidates.append(
                    {
                        "name": text,
                        "address": formatted,
                        "city": str(city_val or province_val or "").strip(),
                        "lat": round(coords[0], 6),
                        "lon": round(coords[1], 6),
                        "provider": "amap",
                    }
                )
        if not candidates:
            _wait_for_queue_slot(interval)
            poi_data = _get_json(
                "https://restapi.amap.com/v3/place/text",
                {
                    "keywords": text,
                    "key": api_key,
                    "offset": limit,
                    "page": 1,
                    "output": "json",
                },
                retries=retries,
            )
            if poi_data and str(poi_data.get("status")) == "1":
                for poi in (poi_data.get("pois") or [])[:limit]:
                    coords = _parse_amap_lonlat(poi.get("location", ""))
                    if not coords:
                        continue
                    pname = str(poi.get("name") or text).strip()
                    cityname = str(poi.get("cityname") or poi.get("pname") or "").strip()
                    adname = str(poi.get("adname") or "").strip()
                    addr = str(poi.get("address") or "").strip()
                    full_addr = "".join(
                        p for p in (cityname, adname, addr if addr != "[]" else "", pname) if p
                    ) or pname
                    candidates.append(
                        {
                            "name": pname,
                            "address": full_addr,
                            "city": cityname,
                            "lat": round(coords[0], 6),
                            "lon": round(coords[1], 6),
                            "provider": "amap",
                        }
                    )
        if candidates or not allow_fallback:
            return candidates

    _wait_for_queue_slot(interval)
    data = _get_json(
        "https://nominatim.openstreetmap.org/search",
        {
            "format": "jsonv2",
            "q": text,
            "accept-language": "zh-CN",
            "addressdetails": 1,
            "limit": limit,
        },
        headers={"User-Agent": "LocalPhotoAlbum/1.0 (personal use)"},
        retries=retries,
    )
    if isinstance(data, list):
        for item in data[:limit]:
            try:
                lat = float(item["lat"])
                lon = float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            addr_obj = item.get("address") or {}
            city = (
                addr_obj.get("city")
                or addr_obj.get("town")
                or addr_obj.get("municipality")
                or addr_obj.get("state")
                or ""
            )
            display = str(item.get("display_name") or item.get("name") or text).strip()
            name = str(item.get("name") or text).strip() or text
            candidates.append(
                {
                    "name": name,
                    "address": display,
                    "city": str(city).strip(),
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "provider": "nominatim",
                }
            )
    return candidates
