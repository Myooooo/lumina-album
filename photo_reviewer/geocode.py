"""Reverse geocoding helpers for turning GPS coordinates into place names."""
from __future__ import annotations

from typing import Optional

import requests


def reverse_geocode(lat: float, lon: float, provider: str = "nominatim", api_key: str = "") -> Optional[str]:
    """Convert coordinates to a human-readable Chinese place name.

    Defaults to Nominatim (free, no API key). If ``provider`` is ``amap`` and
    an API key is provided, uses AMap (高德) reverse geocoding instead.
    """
    try:
        if provider == "amap" and api_key:
            url = "https://restapi.amap.com/v3/geocode/regeo"
            params = {
                "location": f"{lon:.6f},{lat:.6f}",
                "key": api_key,
                "extensions": "base",
                "output": "json",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            regeocode = data.get("regeocode") or {}
            formatted = regeocode.get("formatted_address")
            if formatted:
                return str(formatted).strip()
            address_component = regeocode.get("addressComponent") or {}
            return str(address_component.get(" township") or address_component.get("city") or address_component.get("province") or "").strip() or None

        # Free Nominatim / OpenStreetMap
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            "format": "jsonv2",
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
            "accept-language": "zh-CN",
            "zoom": 16,
        }
        headers = {"User-Agent": "LocalPhotoAlbum/1.0 (personal use)"}
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("display_name") or data.get("name") or None
    except Exception:
        return None
