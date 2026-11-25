"""Unit tests for custom places, forward geocoding, DB location override & footprints API."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# Allow geocode pure-logic tests to run even when optional `requests` is absent,
# without polluting sys.modules for other test modules during unittest discover.
_INJECTED_REQUESTS = False
if "requests" not in sys.modules:
    try:
        import requests  # type: ignore  # noqa: F401
    except ImportError:
        fake_req = types.ModuleType("requests")
        fake_req.__path__ = []  # type: ignore[attr-defined]
        fake_exc = types.SimpleNamespace(RequestException=Exception)
        fake_req.exceptions = fake_exc  # type: ignore[attr-defined]
        fake_req.get = None  # type: ignore[attr-defined]
        fake_adapters = types.ModuleType("requests.adapters")
        fake_adapters.HTTPAdapter = object  # type: ignore[attr-defined]
        sys.modules["requests"] = fake_req
        sys.modules["requests.adapters"] = fake_adapters
        _INJECTED_REQUESTS = True

from photo_reviewer.db import Database
from photo_reviewer.footprints import merge_places_and_footprints, photo_coords
from photo_reviewer.geocode import (
    forward_geocode,
    gcj02_to_wgs84,
    wgs84_to_gcj02,
)

try:
    from photo_reviewer.config import Config
    from photo_reviewer.server import create_app

    _HAS_FLASK_APP = True
except ImportError:
    _HAS_FLASK_APP = False


class FootprintsPlacesTest(unittest.TestCase):
    def test_gcj02_roundtrip_accuracy(self) -> None:
        # Hangzhou West Lake (WGS-84)
        w_lat, w_lon = 30.248963, 120.149751
        g_lat, g_lon = wgs84_to_gcj02(w_lat, w_lon)
        r_lat, r_lon = gcj02_to_wgs84(g_lat, g_lon)
        self.assertAlmostEqual(r_lat, w_lat, places=4)
        self.assertAlmostEqual(r_lon, w_lon, places=4)

        # Outside China (Sydney) stays unchanged
        s_lat, s_lon = -33.8688, 151.2093
        self.assertEqual(gcj02_to_wgs84(s_lat, s_lon), (s_lat, s_lon))

    def test_forward_geocode_providers(self) -> None:
        # Provider "none" makes zero network requests
        with mock.patch("photo_reviewer.geocode._get_json") as m_get:
            self.assertEqual(forward_geocode("西湖", provider="none"), [])
            m_get.assert_not_called()

        # Nominatim search
        nom_resp = [
            {
                "name": "西湖",
                "display_name": "西湖, 西湖区, 杭州市, 浙江省, 中国",
                "lat": "30.248963",
                "lon": "120.149751",
                "address": {"city": "杭州市"},
            }
        ]
        with mock.patch("photo_reviewer.geocode._get_json", return_value=nom_resp):
            cands = forward_geocode("西湖", provider="nominatim", interval=0.1)
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0]["city"], "杭州市")
            self.assertAlmostEqual(cands[0]["lat"], 30.248963, places=5)

        # AMap geocode converts GCJ-02 -> WGS-84
        g_lat, g_lon = wgs84_to_gcj02(30.248963, 120.149751)
        amap_resp = {
            "status": "1",
            "geocodes": [
                {
                    "formatted_address": "浙江省杭州市西湖区西湖",
                    "city": "杭州市",
                    "location": f"{g_lon:.6f},{g_lat:.6f}",
                }
            ],
        }
        with mock.patch("photo_reviewer.geocode._get_json", return_value=amap_resp):
            cands = forward_geocode("杭州西湖", provider="amap", api_key="test-key", interval=0.1)
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0]["provider"], "amap")
            self.assertAlmostEqual(cands[0]["lat"], 30.248963, places=4)
            self.assertAlmostEqual(cands[0]["lon"], 120.149751, places=4)

    def test_db_places_crud_and_location_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "library.db"))
            folder = str(Path(tmp) / "album")
            try:
                p1 = db.upsert_photo(
                    {
                        "path": f"{folder}/bj.jpg",
                        "folder": folder,
                        "filename": "bj.jpg",
                        "location": "北京市海淀区颐和园",
                        "exif": {"latitude": 39.999, "longitude": 116.275, "datetime_original": "2025:05:01 10:00:00"},
                        "status": "analyzed",
                    }
                )
                p2 = db.upsert_photo(
                    {
                        "path": f"{folder}/noloc.jpg",
                        "folder": folder,
                        "filename": "noloc.jpg",
                        "exif": {"datetime_original": "2025:06:10 12:00:00"},
                        "status": "analyzed",
                    }
                )

                pid = db.create_place(
                    name="杭州西湖",
                    city="杭州市",
                    address="浙江省杭州市西湖风景名胜区",
                    lat=30.248963,
                    lon=120.149751,
                )
                self.assertEqual(db.assign_photos_to_place(pid, [p2]), 1)

                row2 = db.get_photo(p2)
                self.assertEqual(row2["place_id"], pid)
                self.assertEqual(row2["location"], "浙江省杭州市西湖风景名胜区")
                self.assertEqual(row2["location_source"], "manual")
                self.assertEqual(photo_coords(row2), (30.248963, 120.149751))
                # EXIF dict stays untouched
                self.assertNotIn("latitude", row2["exif"])

                # Merge places & EXIF footprints without double counting
                merged = merge_places_and_footprints(db.located_photos(folder), db.list_places())
                self.assertEqual(len(merged), 2)
                self.assertEqual(sum(m.count for m in merged), 2)

                # Rename place syncs DB location
                db.update_place(pid, name="西湖断桥", address="杭州市西湖断桥残雪")
                self.assertEqual(db.get_photo(p2)["location"], "杭州市西湖断桥残雪")

                # Remove photo from place clears manual location
                self.assertEqual(db.remove_photos_from_place(pid, [p2]), 1)
                self.assertIsNone(db.get_photo(p2)["place_id"])
                self.assertIsNone(db.get_photo(p2)["location"])

                # Delete place
                self.assertTrue(db.delete_place(pid))
                self.assertIsNone(db.get_place(pid))
            finally:
                try:
                    db.close()
                except ImportError:
                    pass

    @unittest.skipUnless(_HAS_FLASK_APP, "Flask/Pillow not installed in this Python")
    def test_places_http_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(data_dir=tmp)
            db = Database(str(Path(tmp) / "library.db"))
            app = create_app(config=cfg, db=db)
            client = app.test_client()
            folder = str(Path(tmp) / "album")
            try:
                p2 = db.upsert_photo(
                    {
                        "path": f"{folder}/noloc.jpg",
                        "folder": folder,
                        "filename": "noloc.jpg",
                        "exif": {"datetime_original": "2025:06:10 12:00:00"},
                        "status": "analyzed",
                    }
                )
                fake_cands = [
                    {
                        "name": "西湖",
                        "address": "浙江省杭州市西湖风景名胜区",
                        "city": "杭州市",
                        "lat": 30.248963,
                        "lon": 120.149751,
                        "provider": "nominatim",
                    }
                ]
                with mock.patch("photo_reviewer.web.routes_sheets.forward_geocode", return_value=fake_cands):
                    resp = client.post("/api/places", json={"name": "杭州西湖"})
                self.assertEqual(resp.status_code, 201)
                place_id = resp.get_json()["place"]["id"]
                self.assertEqual(
                    client.post(f"/api/places/{place_id}/photos", json={"photo_ids": [p2]}).status_code,
                    200,
                )
                fp = client.get(f"/api/footprints?folder={folder}").get_json()
                self.assertEqual(fp["total"], 1)
                self.assertIsNotNone(fp["bounds"])
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
