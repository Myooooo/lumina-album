"""EXIF extraction for photos."""

from __future__ import annotations

from typing import Any

from PIL import ExifTags, Image


def _decimal_from_dms(dms, ref) -> float | None:
    try:
        d, m, s = [float(x) for x in dms]
    except (TypeError, ValueError):
        return None
    value = d + m / 60.0 + s / 3600.0
    if ref in {"S", "W"}:
        value = -value
    return value


def extract_exif(image_path: str) -> tuple[str | None, dict[str, Any]]:
    """Return (location_string, exif_dict) from a photo file."""
    location = None
    exif_data: dict[str, Any] = {}
    try:
        with Image.open(image_path) as img:
            exif = img.getexif()
            if not exif:
                return location, exif_data

            # Camera / shot info
            def tag_value(key):
                try:
                    return exif.get(key)
                except Exception:
                    return None

            make = tag_value(271)
            model = tag_value(272)
            datetime_original = tag_value(306)
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
                try:
                    num, den = exposure
                    exif_data["exposure"] = f"{num}/{den}s" if den else str(exposure)
                except Exception:
                    exif_data["exposure"] = str(exposure)
            if fnumber:
                try:
                    exif_data["fnumber"] = round(float(fnumber), 1)
                except Exception:
                    exif_data["fnumber"] = str(fnumber)
            if iso:
                try:
                    exif_data["iso"] = int(iso)
                except Exception:
                    exif_data["iso"] = str(iso)
            if focal:
                try:
                    exif_data["focal_length"] = str(focal).replace("/1", "") + "mm"
                except Exception:
                    exif_data["focal_length"] = str(focal)

            # GPS location
            try:
                gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
            except Exception:
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
    except Exception:
        pass
    return location, exif_data
