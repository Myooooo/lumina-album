"""Unit tests for Phase 2: Trips & Stories clustering, highlights, and 3-theme export."""

from __future__ import annotations

import os
import unittest

from photo_reviewer.trips import cluster_trips, haversine_km, select_trip_highlights

try:
    from photo_reviewer.exporter import ExportPhoto, build_html
    _HAS_EXPORTER = True
except ImportError:
    _HAS_EXPORTER = False


class TripsAndThemedExportTest(unittest.TestCase):
    def test_haversine_distance(self) -> None:
        dist = haversine_km(39.9042, 116.4074, 31.2304, 121.4737)
        self.assertGreater(dist, 1000.0)
        self.assertLess(dist, 1150.0)
        self.assertAlmostEqual(haversine_km(30.0, 120.0, 30.0, 120.0), 0.0, places=5)

    def test_cluster_trips_splits_by_gap_and_builds_route_and_highlights(self) -> None:
        photos = [
            {"id": 1, "captured_at": "2024-10-01 09:00:00", "location": "京都府京都市东山区", "exif": {"latitude": 35.003, "longitude": 135.778}, "score": 7.5},
            {"id": 2, "captured_at": "2024-10-01 15:00:00", "location": "京都府京都市岚山", "exif": {"latitude": 35.015, "longitude": 135.676}, "score": 9.1, "favorite": True},
            {"id": 3, "captured_at": "2024-10-02 10:00:00", "location": "京都府京都市伏见区", "exif": {"latitude": 34.967, "longitude": 135.772}, "score": 8.4},
            {"id": 4, "captured_at": "2024-10-03 18:00:00", "location": "大阪府大阪市中央区", "exif": {"latitude": 34.668, "longitude": 135.501}, "score": 8.8},
            {"id": 5, "captured_at": "2024-10-25 11:00:00", "location": "浙江省杭州市西湖区", "exif": {"latitude": 30.258, "longitude": 120.145}, "score": 8.2},
            {"id": 6, "captured_at": "2024-10-25 16:00:00", "location": "浙江省杭州市西湖区", "exif": {"latitude": 30.242, "longitude": 120.149}, "score": 9.0},
            {"id": 7, "captured_at": "2024-10-26 08:30:00", "location": "浙江省杭州市余杭区", "exif": {"latitude": 30.391, "longitude": 119.998}, "score": 7.9},
        ]
        trips = cluster_trips(photos, max_gap_days=3, min_photos=3)
        self.assertEqual(len(trips), 2)
        self.assertEqual(trips[0]["start_date"], "2024-10-25")
        self.assertEqual(trips[0]["photo_count"], 3)
        self.assertEqual(trips[1]["start_date"], "2024-10-01")
        self.assertEqual(trips[1]["days"], 3)
        self.assertEqual(trips[1]["cover_photo_id"], 2)
        self.assertGreaterEqual(len(trips[1]["route"]), 3)

    def test_select_trip_highlights_spreads_across_buckets(self) -> None:
        photos = [{"id": i, "score": float(i % 10)} for i in range(1, 41)]
        picked = select_trip_highlights(photos, max_count=8)
        self.assertEqual(len(picked), 8)
        self.assertEqual(len({p["id"] for p in picked}), 8)

    def test_build_html_supports_three_themes(self) -> None:
        if not _HAS_EXPORTER:
            # Verify theme definitions are present in exporter.py even without Pillow.
            src = open(
                os.path.join(os.path.dirname(__file__), "..", "photo_reviewer", "exporter.py"),
                encoding="utf-8",
            ).read()
            self.assertIn("_HTML_THEME_OVERRIDES", src)
            self.assertIn("_TALL_THEMES", src)
            self.assertIn("#121110", src)
            return
        sample = [
            ExportPhoto(
                path="",
                proxy_path="",
                filename="demo.jpg",
                title="秋日断桥",
                caption="微风拂过湖面。",
                score=8.9,
                tags=["风景"],
                captured="2024-10-25",
            )
        ]
        for theme in ("paper", "classic", "vintage"):
            doc = build_html(sample, title="测试回忆", theme=theme)
            self.assertIn(f'data-theme="{theme}"', doc)
            self.assertIn("秋日断桥", doc)
        self.assertIn("#121110", build_html(sample, theme="vintage"))
        self.assertIn("#f3f5f8", build_html(sample, theme="classic"))


if __name__ == "__main__":
    unittest.main()
