"""Reverse geocoding helpers for turning GPS coordinates into place names."""

from __future__ import annotations

import time
from typing import Any

import requests


def _get_json(
    url: str,
    params: dict,
    headers: dict | None = None,
    retries: int = 2,
    timeout: int = 8,
) -> dict[str, Any] | None:
    """Small retry helper for the two reverse-geocoding providers."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException:
            if attempt >= retries:
                return None
            time.sleep(0.5 * (2**attempt))
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt >= retries:
                return None
            time.sleep(0.5 * (2**attempt))
            continue
        if resp.status_code >= 400:
            return None
        try:
            return resp.json()
        except ValueError:
            return None
    return None


def reverse_geocode(
    lat: float, lon: float, provider: str = "nominatim", api_key: str = ""
) -> str | None:
    """Convert coordinates to a human-readable Chinese place name.

    Defaults to Nominatim (free, no API key). If ``provider`` is ``amap`` and
    an API key is provided, uses AMap (高德) reverse geocoding instead.
    """
    if provider == "amap" and api_key:
        data = _get_json(
            "https://restapi.amap.com/v3/geocode/regeo",
            {
                "location": f"{lon:.6f},{lat:.6f}",
                "key": api_key,
                "extensions": "base",
                "output": "json",
            },
        )
        if not data:
            return None
        regeocode = data.get("regeocode") or {}
        formatted = regeocode.get("formatted_address")
        if formatted:
            return str(formatted).strip()
        address_component = regeocode.get("addressComponent") or {}
        return (
            str(
                address_component.get("township")
                or address_component.get("city")
                or address_component.get("province")
                or ""
            ).strip()
            or None
        )

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
    )
    return (data.get("display_name") or data.get("name") or None) if data else None
