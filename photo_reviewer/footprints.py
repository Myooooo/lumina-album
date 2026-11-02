"""Group the location names into travel-scale places.

The reverse geocoder returns full street addresses ("北京市海淀区甘家口街道玉渊潭公园"),
which is right for a photo's caption and useless for a footprint map - every photo
of one park would be its own dot. A footprint needs the city.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

#: Administrative suffixes, longest first so "特别行政区" wins over "区".
_PROVINCE_SUFFIXES = ("特别行政区", "自治区", "省", "市")
_CITY_SUFFIXES = ("市", "地区", "自治州", "盟")
#: Everything below a district is street-level noise for this purpose.
_DISTRICT_SUFFIXES = ("区", "县", "旗", "市")

#: A name has to be at least this long before the suffixes are trusted, so a
#: place literally called "市" is not trimmed to nothing.
_MIN_NAME = 2


@dataclass
class Footprint:
    """One place, and the photos taken there."""

    name: str
    city: str
    photos: list[dict[str, Any]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.photos)

    @property
    def years(self) -> list[int]:
        """Distinct capture years, oldest first."""
        found = {_capture_year(photo) for photo in self.photos}
        return sorted(year for year in found if year)


def _capture_year(photo: dict[str, Any]) -> int | None:
    """The 4-digit capture year, tolerating a raw JSON ``exif`` column.

    Rows from ``Database`` arrive with ``exif`` decoded; rows straight from a
    cursor do not. Both reach this module, so both are handled here rather than
    making every caller remember.
    """
    exif = photo.get("exif")
    if isinstance(exif, str):
        try:
            exif = json.loads(exif) if exif.strip() else {}
        except ValueError:
            exif = {}
    if not isinstance(exif, dict):
        return None
    raw = exif.get("datetime_original") or exif.get("DateTimeOriginal") or ""
    text = str(raw)[:4]
    return int(text) if text.isdigit() else None


def _strip_suffix(text: str, suffixes) -> str:
    for suffix in suffixes:
        if len(text) > len(suffix) + _MIN_NAME and text.endswith(suffix):
            return text[: -len(suffix)]
    return text


#: A place name that is only a country adds nothing: "中华人民共和国北角咀路" is a
#: street, and showing it next to 北京市 reads as a data error. Those rows are kept
#: under a catch-all so their photos are still counted.
_UNKNOWN_PLACE = "其他地方"

#: Country-level prefixes some geocoders prepend. Stripped before the province is
#: looked for, so "中华人民共和国广东省深圳市…" groups with 深圳市 rather than
#: producing one entry per street under the country.
_COUNTRY_PREFIXES = ("中华人民共和国", "中国", "中华民国")


def _strip_country(text: str) -> str:
    for prefix in _COUNTRY_PREFIXES:
        if text.startswith(prefix):
            rest = text[len(prefix):]
            # Only a prefix if something meaningful follows it; "中华人民共和国"
            # on its own is the country the photo was taken in, and that is the
            # best answer available.
            if len(rest) >= 2:
                return rest
    return text


def city_of(location: str) -> str:
    """The city-level name for a reverse-geocoded address.

    Chinese administrative addresses run province → city → district → street, so
    the city is the second segment once the province is stripped. Overseas names
    have no such structure and are returned whole: guessing at "Paris, France"
    would be worse than leaving it.
    """
    text = unicodedata.normalize("NFKC", (location or "").strip())
    if not text:
        return ""
    # A bare coordinate pair is its own kind of place; leave it as-is so the UI
    # can label it honestly instead of inventing a city.
    if re.fullmatch(r"-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?", text):
        return text

    original = text
    remainder = _strip_country(text)
    if remainder != text and len(remainder) < 2:
        # The address was just the country name.
        return original

    # Province: 北京市 / 河北省 / 香港特别行政区 / 内蒙古自治区
    for suffix in _PROVINCE_SUFFIXES:
        index = remainder.find(suffix)
        # Only treat it as the province when it appears early - a "市" deep in a
        # street address is not the province.
        if 0 <= index <= 8:
            province = remainder[: index + len(suffix)]
            remainder = remainder[index + len(suffix):]
            # A municipality (北京市 / 上海市) or a special administrative region
            # is itself the city: "海淀区" is a district of the city the photo was
            # taken in, not the place someone would say they visited.
            if province.endswith(("市", "特别行政区")):
                return province
            break

    if not remainder:
        return text
    for suffix in _CITY_SUFFIXES:
        index = remainder.find(suffix)
        if 1 <= index <= 8:
            return remainder[: index + len(suffix)]
    # No recognisable city: fall back to the first district, then to the head.
    for suffix in _DISTRICT_SUFFIXES:
        index = remainder.find(suffix)
        if 1 <= index <= 8:
            return remainder[: index + len(suffix)]
    return remainder[:12] or text


def group_by_city(photos: list[dict[str, Any]]) -> list[Footprint]:
    """Cluster ``photos`` into one footprint per city, busiest first."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for photo in photos:
        location = photo.get("location") or ""
        if not location.strip():
            continue
        bucket = city_of(location)
        if not bucket:
            continue
        buckets.setdefault(bucket, []).append(photo)

    footprints = [
        Footprint(
            name=name,
            city=name,
            photos=sorted(items, key=lambda p: p["id"]),
        )
        for name, items in buckets.items()
    ]
    footprints.sort(key=lambda f: (-f.count, f.city))
    return footprints
