"""Unit tests for Phase 5: photography stats report, HDR tonemap filter & LAN readonly guard."""

from __future__ import annotations

import unittest

from photo_reviewer.stats_report import build_shooting_report, focal_bucket, parse_focal_mm
from photo_reviewer.video import hdr_tonemap_vf


class Phase5AnalyticsAndVideoTest(unittest.TestCase):
    def test_focal_length_parsing_and_buckets(self) -> None:
        self.assertEqual(parse_focal_mm("35 mm"), 35.0)
        self.assertEqual(parse_focal_mm(85), 85.0)
        self.assertEqual(focal_bucket(16), "超广角 (<24mm)")
        self.assertEqual(focal_bucket(35), "广角 (24-35mm)")
        self.assertEqual(focal_bucket(50), "标准 (36-70mm)")
        self.assertEqual(focal_bucket(85), "中长焦 (71-135mm)")
        self.assertEqual(focal_bucket(200), "长焦 (>135mm)")

    def test_build_shooting_report_aggregates_gear_and_months(self) -> None:
        photos = [
            {
                "id": 1,
                "captured_at": "2024-05-10 12:00:00",
                "score": 9.0,
                "favorite": True,
                "location": "浙江省杭州市西湖区",
                "tags": ["风景", "西湖"],
                "exif": {"model": "Sony A7M4", "lens_model": "FE 35mm F1.4 GM", "focal_length_35mm": 35},
            },
            {
                "id": 2,
                "captured_at": "2024-10-02 18:00:00",
                "score": 8.0,
                "favorite": False,
                "location": "京都府京都市东山区",
                "tags": ["风景"],
                "exif": {"model": "Sony A7M4", "lens_model": "FE 85mm F1.4 GM", "focal_length_35mm": 85},
            },
            {
                "id": 3,
                "captured_at": "2025-01-15 09:00:00",
                "score": 8.6,
                "media_kind": "video",
                "tags": ["旅行"],
                "exif": {"model": "iPhone 16 Pro", "focal_length_35mm": 24},
            },
        ]
        all_rep = build_shooting_report(photos)
        self.assertEqual(all_rep["available_years"], [2025, 2024])
        self.assertEqual(all_rep["summary"]["total"], 3)
        self.assertEqual(all_rep["summary"]["videos"], 1)
        self.assertEqual(all_rep["cameras"][0]["name"], "Sony A7M4")
        self.assertEqual(all_rep["cameras"][0]["count"], 2)
        self.assertEqual(all_rep["cameras"][0]["avg_score"], 8.5)

        rep_2024 = build_shooting_report(photos, year=2024)
        self.assertEqual(rep_2024["summary"]["total"], 2)
        self.assertEqual(len(rep_2024["months"]), 12)
        self.assertEqual(rep_2024["months"][4]["count"], 1)  # May
        self.assertEqual(rep_2024["months"][9]["count"], 1)  # Oct

    def test_hdr_tonemap_vf(self) -> None:
        hdr_vf = hdr_tonemap_vf("smpte2084", max_edge=1280)
        self.assertIn("tonemap=tonemap=hable", hdr_vf)
        self.assertIn("zscale=p=bt709", hdr_vf)
        sdr_vf = hdr_tonemap_vf("bt709", max_edge=1280)
        self.assertNotIn("tonemap", sdr_vf)
        self.assertIn("scale=", sdr_vf)


if __name__ == "__main__":
    unittest.main()
