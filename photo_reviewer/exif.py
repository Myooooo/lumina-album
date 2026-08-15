"""EXIF extraction for photos."""

from __future__ import annotations

import logging
import struct
from typing import Any

from PIL import ExifTags, Image

logger = logging.getLogger(__name__)


def _decimal_from_dms(dms, ref) -> float | None:
    try:
        d, m, s = [float(x) for x in dms]
    except (TypeError, ValueError):
        return None
    value = d + m / 60.0 + s / 3600.0
    if ref in {"S", "W"}:
        value = -value
    return value


def _rational_float(value) -> float | None:
    """Convert a PIL/EXIF rational (tuple, float or int) to a float."""
    if isinstance(value, tuple) and len(value) == 2:
        try:
            num, den = float(value[0]), float(value[1])
            return num / den if den else None
        except (TypeError, ValueError):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_exif(image_path: str) -> tuple[str | None, dict[str, Any]]:
    """Return (location_string, exif_dict) from a photo file."""
    location = None
    exif_data: dict[str, Any] = {}
    try:
        with Image.open(image_path) as img:
            exif = img.getexif()
            if not exif:
                return location, exif_data

            # Some phones keep exposure/aperture/ISO/focal-length in the Exif
            # sub-IFD rather than the top-level tags.
            sub_exif = exif.get_ifd(ExifTags.IFD.Exif) or {}

            def tag_value(key):
                try:
                    value = sub_exif.get(key)
                    if value is None:
                        value = exif.get(key)
                    return value
                except (KeyError, TypeError, ValueError):
                    return None

            make = tag_value(271)
            model = tag_value(272)
            datetime_original = tag_value(36867) or tag_value(306)
            exposure = tag_value(33434)
            fnumber = tag_value(33437)
            iso = tag_value(34855)
            focal = tag_value(37386)
            lens = tag_value(42036)

            if make:
                exif_data["make"] = str(make).strip()
            if model:
                exif_data["model"] = str(model).strip()
            if lens:
                exif_data["lens_model"] = str(lens).strip()
            if datetime_original:
                exif_data["datetime_original"] = str(datetime_original).strip()
            if exposure:
                exposure_value = _rational_float(exposure)
                if exposure_value is not None and exposure_value > 0:
                    if exposure_value < 1:
                        exif_data["exposure"] = (
                            f"1/{max(1, round(1 / exposure_value))}s"
                        )
                    else:
                        exif_data["exposure"] = f"{exposure_value:g}s"
                else:
                    exif_data["exposure"] = str(exposure)
            if fnumber:
                fnumber_value = _rational_float(fnumber)
                if fnumber_value is not None:
                    exif_data["fnumber"] = round(fnumber_value, 1)
                else:
                    exif_data["fnumber"] = str(fnumber)
            if iso:
                try:
                    exif_data["iso"] = int(iso)
                except (TypeError, ValueError):
                    exif_data["iso"] = str(iso)
            if focal:
                focal_value = _rational_float(focal)
                if focal_value is not None:
                    focal_text = f"{focal_value:g}"
                else:
                    focal_text = str(focal).replace("/1", "")
                exif_data["focal_length"] = f"{focal_text}mm"

            try:
                gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
            except (KeyError, TypeError, ValueError, struct.error):
                gps_ifd = None
            if gps_ifd:
                lat_ref = gps_ifd.get(1)
                lat_dms = gps_ifd.get(2)
                lon_ref = gps_ifd.get(3)
                lon_dms = gps_ifd.get(4)
                lat = _decimal_from_dms(lat_dms, lat_ref) if lat_dms else None
                lon = _decimal_from_dms(lon_dms, lon_ref) if lon_dms else None
                if lat is not None and lon is not None:
                    location = f"{lat:.5f}, {lon:.5f}"
                    exif_data["latitude"] = lat
                    exif_data["longitude"] = lon
                    exif_data["location"] = location
    except Exception as exc:  # noqa: BLE001 - EXIF readers can raise arbitrary codec errors
        logger.debug("EXIF extraction failed for %s: %s", image_path, exc)
    return location, exif_data
