"""Core regression tests for Lumina Album.

These tests run fully locally: all databases and image folders are temporary,
and model calls are replaced with deterministic fakes.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from photo_reviewer.api_client import (
    SemanticSearchError,
    _parse_model_json,
    analyze_image,
)
from photo_reviewer.config import Config
from photo_reviewer.db import Database, parse_capture_datetime
from photo_reviewer.scanner import JOBS, start_scan
from photo_reviewer.server import create_app


def wait_for_job(job_id: str, timeout: float = 6.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = JOBS.get(job_id)
        if job and job.status in ("completed", "cancelled", "error"):
            return job
        time.sleep(0.02)
    raise TimeoutError(f"job {job_id} did not finish")


def fake_analysis(proxy_path: str, config: Config, context=None) -> dict:
    return {
        "score": 7.5,
        "title": "温柔的黄昏",
        "dimensions": {"technical": 7, "composition": 8, "memory": 9, "uniqueness": 6},
        "tags": ["风景", "黄昏"],
        "comment": "温柔的一刻",
    }


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "library.db"))

    def tearDown(self) -> None:
        self.db = None
        self.tmp.cleanup()

    def test_capture_time_sort_puts_missing_dates_last(self) -> None:
        folder = os.path.join(self.tmp.name, "pics")
        rows = [
            ("2023:05:06 07:08:09", 5.0, "b.jpg"),
            (None, 9.0, "a.jpg"),
            ("2021-01-02 03:04:05", 2.0, "c.jpg"),
            ("2024-12-31 23:59:59", 7.0, "d.jpg"),
        ]
        for idx, (captured, score, filename) in enumerate(rows):
            exif = {"datetime_original": captured} if captured else {}
            self.db.upsert_photo(
                {
                    "path": os.path.join(folder, filename),
                    "folder": folder,
                    "filename": filename,
                    "size": idx,
                    "status": "analyzed",
                    "score": score,
                    "exif": exif,
                    "tags": [],
                }
            )

        ascending = [
            p["filename"] for p in self.db.list_photos(folder=folder, sort="time_asc")
        ]
        self.assertEqual(ascending, ["c.jpg", "b.jpg", "d.jpg", "a.jpg"])
        descending = [
            p["filename"] for p in self.db.list_photos(folder=folder, sort="time_desc")
        ]
        self.assertEqual(descending, ["d.jpg", "b.jpg", "c.jpg", "a.jpg"])
        self.assertEqual(self.db.capture_years(folder), [2024, 2023, 2021])
        self.assertEqual(
            [p["filename"] for p in self.db.list_photos(folder=folder, year=2021)],
            ["c.jpg"],
        )

    def test_cache_paths_are_scoped_and_persistable(self) -> None:
        folder_a = os.path.join(self.tmp.name, "a")
        folder_b = os.path.join(self.tmp.name, "b")
        self.db.upsert_photo(
            {
                "path": os.path.join(folder_a, "1.jpg"),
                "folder": folder_a,
                "filename": "1.jpg",
                "proxy_path": os.path.join(folder_a, ".cache", "1_proxy.jpg"),
                "thumb_path": os.path.join(folder_a, ".cache", "1_proxy.jpg"),
            }
        )
        self.db.upsert_photo(
            {
                "path": os.path.join(folder_b, "2.jpg"),
                "folder": folder_b,
                "filename": "2.jpg",
                "proxy_path": os.path.join(folder_b, ".cache", "2_proxy.jpg"),
            }
        )
        self.assertEqual(len(self.db.cache_paths_for_folder(folder_a)), 1)
        self.assertEqual(len(self.db.cache_paths_for_folder(folder_b)), 1)
        photo_id = self.db.get_photo_by_path(os.path.join(folder_a, "1.jpg"))["id"]
        self.db.update_cache_paths(
            photo_id,
            os.path.join(folder_a, ".cache", "new_thumb.jpg"),
            os.path.join(folder_a, ".cache", "new.jpg"),
        )
        paths = self.db.cache_paths_for_folder(folder_a)[0]
        self.assertEqual(
            paths["proxy_path"], os.path.join(folder_a, ".cache", "new.jpg")
        )
        self.assertEqual(
            paths["thumb_path"], os.path.join(folder_a, ".cache", "new_thumb.jpg")
        )

    def test_edit_metadata_and_remove_folder(self) -> None:
        folder = os.path.join(self.tmp.name, "pics")
        photo_path = os.path.join(folder, "1.jpg")
        self.db.upsert_photo(
            {
                "path": photo_path,
                "folder": folder,
                "filename": "1.jpg",
                "status": "analyzed",
                "score": 5.0,
                "dimensions": {"technical": 5},
                "tags": ["旧"],
                "reason": "旧评语",
            }
        )
        photo_id = self.db.get_photo_by_path(photo_path)["id"]
        self.db.update_metadata(
            photo_id,
            9.5,
            {"technical": 9, "composition": 8, "memory": 7, "uniqueness": 6},
            ["新标签", "风景"],
            "新评语",
            "新标题",
            "杭州",
        )
        photo = self.db.get_photo(photo_id)
        self.assertEqual(photo["score"], 9.5)
        self.assertEqual(photo["dimensions"]["memory"], 7)
        self.assertEqual(photo["tags"], ["新标签", "风景"])
        self.assertEqual(photo["title"], "新标题")
        self.assertEqual(self.db.remove_folder(folder), 1)
        self.assertIsNone(self.db.get_photo(photo_id))

    def test_all_folders_returns_most_recent_first(self) -> None:
        for idx, folder in enumerate(["older", "newer"]):
            path = os.path.join(self.tmp.name, folder, "1.jpg")
            self.db.upsert_photo(
                {
                    "path": path,
                    "folder": folder,
                    "filename": "1.jpg",
                    "status": "analyzed",
                }
            )
        # Re-upserting newer refreshes created_at, making it first.
        self.db.upsert_photo(
            {
                "path": os.path.join(self.tmp.name, "newer", "1.jpg"),
                "folder": os.path.join(self.tmp.name, "newer"),
                "filename": "1.jpg",
                "status": "analyzed",
            }
        )
        self.assertEqual(self.db.all_folders()[0], os.path.join(self.tmp.name, "newer"))

    def test_resolved_location_is_preserved_over_raw_coordinates(self) -> None:
        from photo_reviewer.scanner import _preserve_resolved_location

        self.assertEqual(
            _preserve_resolved_location("30.25000, 120.17000", {"location": "杭州"}),
            "杭州",
        )
        self.assertEqual(
            _preserve_resolved_location(
                "30.25000, 120.17000", {"location": "30.1, 120.2"}
            ),
            "30.25000, 120.17000",
        )

    def test_extract_exif_reads_phone_sub_ifd(self) -> None:
        from unittest.mock import patch

        from PIL import ExifTags

        from photo_reviewer.exif import extract_exif

        class FakeExif:
            def get(self, key):
                return {"271": "HUAWEI", "272": "LIO-AN00"}.get(str(key))

            def get_ifd(self, ifd):
                if ifd == ExifTags.IFD.Exif:
                    return {
                        33434: 0.000942,
                        33437: 1.6,
                        34855: 50,
                        37386: 5.56,
                        36867: "2020:08:19 16:45:51",
                    }
                return {}

        class FakeImage:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def getexif(self):
                return FakeExif()

        with patch("PIL.Image.open", return_value=FakeImage()):
            _, exif = extract_exif("fake.jpg")
        self.assertEqual(exif["make"], "HUAWEI")
        self.assertEqual(exif["exposure"], "1/1062s")
        self.assertEqual(exif["fnumber"], 1.6)
        self.assertEqual(exif["iso"], 50)
        self.assertEqual(exif["focal_length"], "5.56mm")

    def test_resolve_location_keeps_old_address_when_geocoding_fails(self) -> None:
        from unittest.mock import patch

        from photo_reviewer.config import Config
        from photo_reviewer.scanner import _resolve_location

        cfg = Config(data_dir=self.tmp.name)
        with patch("photo_reviewer.scanner.reverse_geocode", return_value=None):
            result = _resolve_location(
                "30.25000, 120.17000",
                {"latitude": 30.25, "longitude": 120.17},
                {"location": "杭州"},
                cfg,
            )
        self.assertEqual(result, "杭州")

    def test_model_retry_defaults_to_three(self) -> None:
        from photo_reviewer.config import Config

        cfg = Config(data_dir=self.tmp.name)
        self.assertEqual(cfg.model_retries, 3)
        self.assertEqual(cfg.scan_concurrency, 1)

    def test_parse_capture_datetime_variants(self) -> None:
        self.assertIsNotNone(parse_capture_datetime("2023:01:02 03:04:05"))
        self.assertIsNotNone(parse_capture_datetime("2023-01-02"))
        self.assertIsNone(parse_capture_datetime("not-a-date"))


class ApiClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=self.tmp.name, model_retries=1)
        self.image_path = os.path.join(self.tmp.name, "fake.jpg")
        Path(self.image_path).write_bytes(b"\xff\xd8\xff\xd9")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_title_is_forward_compatible(self) -> None:
        from photo_reviewer.api_client import _validate_result

        result = {"score": 6, "tags": ["旧"], "comment": "旧输出"}
        _validate_result(result)
        self.assertEqual(result["title"], "")

    def test_parse_fenced_and_bare_json(self) -> None:
        self.assertEqual(_parse_model_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(_parse_model_json('{"a": 1}'), {"a": 1})

    def test_retry_on_malformed_json_then_success(self) -> None:
        bad = Mock(status_code=200)
        bad.json.return_value = {"choices": [{"message": {"content": "not-json"}}]}
        good = Mock(status_code=200)
        good.json.return_value = {
            "choices": [
                {"message": {"content": json.dumps(fake_analysis("", self.cfg))}}
            ]
        }
        with (
            patch(
                "photo_reviewer.api_client.requests.post", side_effect=[bad, good]
            ) as post,
            patch("photo_reviewer.api_client.time.sleep") as sleep,
        ):
            result = analyze_image(self.image_path, self.cfg)
            self.assertEqual(result["score"], 7.5)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(sleep.call_args_list[0][0][0], 1.0)

    def test_semantic_search_failure_is_explicit(self) -> None:
        from photo_reviewer.api_client import semantic_search

        with (
            patch(
                "photo_reviewer.api_client.requests.post", side_effect=OSError("down")
            ),
            self.assertRaises(SemanticSearchError),
        ):
            semantic_search(
                "夏天",
                [
                    {
                        "id": 1,
                        "filename": "a.jpg",
                        "tags": [],
                        "reason": "",
                        "location": "",
                    }
                ],
                self.cfg,
            )


class GeocodeTests(unittest.TestCase):
    def test_amap_uses_formatted_address(self) -> None:
        from unittest.mock import patch

        from photo_reviewer.geocode import reverse_geocode

        payload = {
            "status": "1",
            "info": "OK",
            "regeocode": {"formatted_address": "北京市朝阳区望京街道方恒国际"},
        }
        with patch("photo_reviewer.geocode._get_json", return_value=payload):
            result = reverse_geocode(
                39.990464, 116.481488, provider="amap", api_key="test-key", interval=0.1
            )
        self.assertEqual(result, "北京市朝阳区望京街道方恒国际")

    def test_amap_request_uses_longitude_first(self) -> None:
        from unittest.mock import patch

        from photo_reviewer.geocode import reverse_geocode, wgs84_to_gcj02

        captured = {}

        def fake_get_json(url, params, **kwargs):
            captured.update(params)
            return {"status": "1", "regeocode": {"formatted_address": "测试地址"}}

        with patch("photo_reviewer.geocode._get_json", side_effect=fake_get_json):
            reverse_geocode(
                30.25, 120.17, provider="amap", api_key="k", interval=0.1, retries=1
            )
        expected_lat, expected_lon = wgs84_to_gcj02(30.25, 120.17)
        self.assertEqual(captured["location"], f"{expected_lon:.6f},{expected_lat:.6f}")

    def test_amap_failure_falls_back_to_nominatim(self) -> None:
        from unittest.mock import patch

        from photo_reviewer.geocode import reverse_geocode

        with patch(
            "photo_reviewer.geocode._get_json",
            side_effect=[None, {"display_name": "杭州市西湖区"}],
        ) as get_json:
            result = reverse_geocode(
                30.25,
                120.17,
                provider="amap",
                api_key="bad-key",
                interval=0.1,
                retries=2,
            )
        self.assertEqual(result, "杭州市西湖区")
        self.assertEqual(get_json.call_count, 2)
        self.assertIn("nominatim", get_json.call_args_list[1].args[0])

    def test_geocoding_retries_non_200(self) -> None:
        from unittest.mock import Mock, patch

        from photo_reviewer.geocode import reverse_geocode

        responses = [Mock(status_code=500), Mock(status_code=503)]
        good = Mock(status_code=200)
        good.json.return_value = {"display_name": "上海市黄浦区"}
        responses.append(good)
        with (
            patch("photo_reviewer.geocode.requests.get", side_effect=responses),
            patch("photo_reviewer.geocode.time.sleep") as sleep,
        ):
            result = reverse_geocode(
                31.23,
                121.47,
                provider="nominatim",
                interval=0.1,
                retries=2,
            )
        self.assertEqual(result, "上海市黄浦区")
        self.assertGreaterEqual(sleep.call_count, 2)

    def test_amap_error_status_returns_none(self) -> None:
        from unittest.mock import patch

        from photo_reviewer.geocode import reverse_geocode

        with patch(
            "photo_reviewer.geocode._get_json",
            return_value={"status": "0", "info": "INVALID_USER_KEY"},
        ):
            result = reverse_geocode(
                39.990464, 116.481488, provider="amap", api_key="bad", interval=0.1
            )
        self.assertIsNone(result)


class ThumbnailerTests(unittest.TestCase):
    def test_make_proxy_creates_small_gallery_thumb(self) -> None:
        import tempfile

        from photo_reviewer.thumbnailer import make_proxy

        with tempfile.TemporaryDirectory() as tmp:
            image_path = os.path.join(tmp, "large.jpg")
            Image.new("RGB", (1600, 1200)).save(image_path)
            cache_dir = os.path.join(tmp, ".cache")
            proxy, _, _, _, thumb = make_proxy(
                image_path,
                max_edge=1024,
                cache_dir=cache_dir,
                quality=85,
                thumb_size=320,
            )
            self.assertTrue(proxy.endswith("_proxy.jpg"))
            self.assertTrue(thumb.endswith("_thumb.jpg"))
            self.assertTrue(Path(proxy).exists())
            self.assertTrue(Path(thumb).exists())
            with Image.open(thumb) as img:
                self.assertLessEqual(max(img.size), 320)


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_index_phase_uses_configured_concurrency(self) -> None:
        self.cfg.index_concurrency = 2
        for idx in range(4):
            Image.new("RGB", (32, 32)).save(os.path.join(self.folder, f"{idx}.jpg"))
        active = [0]
        peak = [0]
        original_prepare_proxy = None
        from photo_reviewer import scanner as scanner_module

        original_prepare_proxy = scanner_module.prepare_proxy

        def slow_prepare(image_path, folder, config):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.08)
            try:
                return original_prepare_proxy(image_path, folder, config)
            finally:
                active[0] -= 1

        with (
            patch("photo_reviewer.scanner.prepare_proxy", side_effect=slow_prepare),
            patch("photo_reviewer.scanner.analyze_prepared", side_effect=fake_analysis),
        ):
            job = start_scan(self.folder, self.db, self.cfg)
            result = wait_for_job(job.id)
        self.assertEqual(result.status, "completed")
        self.assertGreaterEqual(peak[0], 2)

    def test_scan_analyzes_images_and_marks_corrupt_files(self) -> None:
        Image.new("RGB", (64, 64), (180, 120, 80)).save(
            os.path.join(self.folder, "good.jpg")
        )
        Path(os.path.join(self.folder, "bad.jpg")).write_bytes(b"not-an-image")
        with patch(
            "photo_reviewer.scanner.analyze_prepared", side_effect=fake_analysis
        ):
            job = start_scan(self.folder, self.db, self.cfg)
            result = wait_for_job(job.id)
        self.assertEqual(result.status, "completed")
        photos = self.db.list_photos(folder=self.folder, exclude_deleted=True)
        by_name = {p["filename"]: p for p in photos}
        self.assertEqual(by_name["good.jpg"]["status"], "analyzed")
        self.assertEqual(by_name["good.jpg"]["score"], 7.5)
        self.assertEqual(by_name["good.jpg"]["title"], "温柔的黄昏")
        self.assertEqual(by_name["bad.jpg"]["status"], "error")
        self.assertIn("无法读取", by_name["bad.jpg"]["error"])

    def test_rebuild_index_skips_existing_and_indexes_new_exif(self) -> None:
        # Existing indexed photo must stay untouched.
        existing_path = os.path.join(self.folder, "existing.jpg")
        Image.new("RGB", (40, 40)).save(existing_path)
        self.db.upsert_photo(
            {
                "path": existing_path,
                "folder": self.folder,
                "filename": "existing.jpg",
                "status": "analyzed",
                "score": 5,
                "exif": {"make": "Old"},
                "location": "杭州",
            }
        )

        # Newly discovered photo should get proxy + EXIF.
        new_path = os.path.join(self.folder, "new.jpg")
        img = Image.new("RGB", (40, 40))
        exif = Image.Exif()
        exif[271] = "Canon"
        exif[272] = "EOS R"
        exif[33437] = (28, 10)
        exif[33434] = (1, 125)
        exif[34855] = 100
        exif[37386] = (50, 1)
        img.save(new_path, exif=exif)

        from photo_reviewer.scanner import start_rebuild_index

        job = start_rebuild_index(self.folder, self.db, self.cfg)
        wait_for_job(job.id)

        old_row = self.db.get_photo_by_path(existing_path)
        self.assertEqual(old_row["exif"], {"make": "Old"})
        self.assertEqual(old_row["location"], "杭州")

        new_row = self.db.get_photo_by_path(new_path)
        self.assertEqual(new_row["exif"]["fnumber"], 2.8)
        self.assertEqual(new_row["exif"]["exposure"], "1/125s")
        self.assertEqual(new_row["exif"]["iso"], 100)
        self.assertEqual(new_row["exif"]["focal_length"], "50mm")

    def test_force_rebuild_refreshes_exif_and_keeps_model_data(self) -> None:
        image_path = os.path.join(self.folder, "camera.jpg")
        img = Image.new("RGB", (40, 40))
        exif = Image.Exif()
        exif[271] = "Canon"
        exif[272] = "EOS R"
        exif[33437] = (28, 10)
        exif[33434] = (1, 125)
        exif[34855] = 100
        exif[37386] = (50, 1)
        img.save(image_path, exif=exif)
        self.db.upsert_photo(
            {
                "path": image_path,
                "folder": self.folder,
                "filename": "camera.jpg",
                "status": "analyzed",
                "score": 8.8,
                "title": "旧标题",
                "tags": ["旧标签"],
                "reason": "旧评语",
                "dimensions": {"technical": 9},
                "model": "old-model",
                "favorite": 1,
                "exif": {},
                "location": "杭州",
            }
        )
        from photo_reviewer.scanner import start_rebuild_index

        with patch("photo_reviewer.scanner.analyze_prepared") as analyze:
            job = start_rebuild_index(self.folder, self.db, self.cfg, force=True)
            wait_for_job(job.id)
            analyze.assert_not_called()

        row = self.db.get_photo_by_path(image_path)
        self.assertEqual(row["exif"]["fnumber"], 2.8)
        self.assertEqual(row["exif"]["iso"], 100)
        self.assertEqual(row["score"], 8.8)
        self.assertEqual(row["title"], "旧标题")
        self.assertEqual(row["tags"], ["旧标签"])
        self.assertEqual(row["reason"], "旧评语")
        self.assertEqual(row["dimensions"], {"technical": 9})
        self.assertEqual(row["model"], "old-model")
        self.assertEqual(row["favorite"], 1)
        self.assertEqual(row["location"], "杭州")

    def test_rebuild_without_changes_has_nonzero_progress(self) -> None:
        image_path = os.path.join(self.folder, "existing.jpg")
        Image.new("RGB", (40, 40)).save(image_path)
        self.db.upsert_photo(
            {
                "path": image_path,
                "folder": self.folder,
                "filename": "existing.jpg",
                "status": "analyzed",
                "score": 5,
            }
        )
        from photo_reviewer.scanner import start_rebuild_index

        job = start_rebuild_index(self.folder, self.db, self.cfg)
        result = wait_for_job(job.id)
        self.assertEqual(result.status, "completed")
        self.assertGreaterEqual(result.total, 1)
        self.assertGreaterEqual(result.processed, 1)

    def test_initial_scan_resolves_location_once_during_indexing(self) -> None:
        image_path = os.path.join(self.folder, "gps.jpg")
        Image.new("RGB", (40, 40)).save(image_path)
        calls = []

        def fake_extract(path):
            return ("30.25000, 120.17000", {"latitude": 30.25, "longitude": 120.17})

        def fake_geocode(lat, lon, provider, api_key, interval=1.0, retries=3):
            calls.append((lat, lon, interval, retries))
            return "杭州"

        with (
            patch("photo_reviewer.scanner.extract_exif", side_effect=fake_extract),
            patch("photo_reviewer.scanner.reverse_geocode", side_effect=fake_geocode),
            patch("photo_reviewer.scanner.analyze_prepared", side_effect=fake_analysis),
        ):
            job = start_scan(self.folder, self.db, self.cfg)
            wait_for_job(job.id)

        row = self.db.get_photo_by_path(image_path)
        self.assertEqual(row["location"], "杭州")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], self.cfg.geocoding_interval)
        self.assertEqual(calls[0][3], self.cfg.geocoding_retries)

    def test_reanalyze_syncs_exif_before_model_call(self) -> None:
        image_path = os.path.join(self.folder, "camera.jpg")
        img = Image.new("RGB", (40, 40))
        exif = Image.Exif()
        exif[271] = "Canon"
        exif[272] = "EOS R"
        exif[33437] = (28, 10)
        exif[33434] = (1, 125)
        exif[34855] = 100
        exif[37386] = (50, 1)
        img.save(image_path, exif=exif)
        self.db.upsert_photo(
            {
                "path": image_path,
                "folder": self.folder,
                "filename": "camera.jpg",
                "status": "analyzed",
                "score": 5,
                "exif": {},
                "location": "杭州",
            }
        )
        photo_id = self.db.get_photo_by_path(image_path)["id"]
        from photo_reviewer.scanner import reanalyze_photo

        with (
            patch(
                "photo_reviewer.scanner.analyze_prepared",
                side_effect=RuntimeError("model offline"),
            ),
            self.assertRaises(RuntimeError),
        ):
            reanalyze_photo(photo_id, self.db, self.cfg)
        row = self.db.get_photo(photo_id)
        self.assertEqual(row["exif"]["fnumber"], 2.8)
        self.assertEqual(row["exif"]["exposure"], "1/125s")
        self.assertEqual(row["exif"]["iso"], 100)
        self.assertEqual(row["location"], "杭州")


class ServerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.db.upsert_photo(
            {
                "path": os.path.join(self.folder, "a.jpg"),
                "folder": self.folder,
                "filename": "a.jpg",
                "status": "analyzed",
                "score": 8.0,
                "tags": ["夏日"],
                "reason": "海边",
            }
        )
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_smart_search_falls_back_with_warning(self) -> None:
        with patch(
            "photo_reviewer.server.semantic_search",
            side_effect=SemanticSearchError("模型离线"),
        ):
            resp = self.client.post(
                "/api/search",
                json={
                    "folder": self.folder,
                    "query": "夏日",
                    "mode": "smart",
                    "status": "analyzed",
                },
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual([p["filename"] for p in data["photos"]], ["a.jpg"])
        self.assertTrue(data["warning"])

    def test_edit_endpoint_updates_metadata(self) -> None:
        photo_id = self.db.get_photo_by_path(os.path.join(self.folder, "a.jpg"))["id"]
        resp = self.client.post(
            f"/api/photo/{photo_id}/edit",
            json={
                "score": 9.2,
                "dimensions": {
                    "technical": 9,
                    "composition": 8,
                    "memory": 9,
                    "uniqueness": 7,
                },
                "tags": ["夏日", "海边"],
                "title": "夏日海边",
                "reason": "新的回忆",
                "location": "青岛",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["score"], 9.2)
        self.assertEqual(data["tags"], ["夏日", "海边"])
        self.assertEqual(data["title"], "夏日海边")
        self.assertEqual(data["location"], "青岛")

    def test_remove_folder_clears_cache_but_keeps_photos(self) -> None:
        photo_path = os.path.join(self.folder, "a.jpg")
        Path(photo_path).write_bytes(b"original")
        cache_dir = Path(self.folder) / self.cfg.cache_dir_name
        cache_dir.mkdir()
        (cache_dir / "dummy_proxy.jpg").write_bytes(b"cache")
        resp = self.client.post("/api/folder/remove", json={"folder": self.folder})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["removed"], 1)
        self.assertFalse(cache_dir.exists())
        self.assertTrue(Path(photo_path).exists())
        self.assertEqual(self.db.stats(self.folder)["total"], 0)

    def test_empty_trash_deletes_files_and_rows(self) -> None:
        photo_path = os.path.join(self.folder, "a.jpg")
        Path(photo_path).write_bytes(b"original")
        trash_dir = Path(self.folder) / self.cfg.trash_dir_name
        trash_dir.mkdir()
        trash_path = trash_dir / "a.jpg"
        trash_path.write_bytes(b"trash-file")
        photo_id = self.db.get_photo_by_path(photo_path)["id"]
        os.remove(photo_path)  # mimic _move_to_trash: original path is now empty
        self.db.mark_deleted([photo_id], {photo_id: str(trash_path)})
        resp = self.client.post("/api/trash/empty", json={"folder": self.folder})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["deleted"], 1)
        self.assertFalse(trash_path.exists())
        self.assertIsNone(self.db.get_photo(photo_id))
        # The original path was already moved away before this test.

    def test_photos_endpoint_supports_pagination(self) -> None:
        for idx in range(3):
            self.db.upsert_photo(
                {
                    "path": os.path.join(self.folder, f"{idx}.jpg"),
                    "folder": self.folder,
                    "filename": f"{idx}.jpg",
                    "status": "analyzed",
                    "score": 8 - idx,
                }
            )
        first = self.client.get(f"/api/photos?folder={self.folder}&limit=2").get_json()
        self.assertEqual(len(first["photos"]), 2)
        self.assertTrue(first["has_more"])
        second = self.client.get(
            f"/api/photos?folder={self.folder}&limit=2&offset=2"
        ).get_json()
        self.assertEqual(len(second["photos"]), 2)
        self.assertFalse(second["has_more"])

    def test_prompt_presets_are_available(self) -> None:
        data = self.client.get("/api/prompt-presets").get_json()
        self.assertEqual(data["default"], "playful")
        for key in ("playful", "literary", "melancholy", "humorous", "warm"):
            self.assertTrue(data["presets"][key].strip())

    def test_model_queue_and_retry_settings_persist(self) -> None:
        resp = self.client.post(
            "/api/config",
            json={
                "scan_concurrency": 2,
                "model_retries": 4,
                "gallery_thumb_size": 320,
            },
        )
        self.assertEqual(resp.status_code, 200)
        cfg = self.client.get("/api/config").get_json()
        self.assertEqual(cfg["scan_concurrency"], 2)
        self.assertEqual(cfg["model_retries"], 4)
        self.assertEqual(cfg["gallery_thumb_size"], 320)

    def test_scan_jobs_endpoint_reports_running_job(self) -> None:
        from photo_reviewer.scanner import JOBS

        job = JOBS.create(self.folder, False)
        JOBS.update(
            job.id,
            status="running",
            phase="analyze",
            total=10,
            processed=3,
            current="working.jpg",
        )
        data = self.client.get(f"/api/scan/jobs?folder={self.folder}").get_json()
        running = [item for item in data["jobs"] if item["job_id"] == job.id]
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["status"], "running")
        self.assertEqual(running[0]["processed"], 3)

    def test_proxy_endpoint_serves_proxy_image(self) -> None:
        proxy_dir = Path(self.folder) / self.cfg.cache_dir_name
        proxy_dir.mkdir()
        proxy_path = proxy_dir / "1_proxy.jpg"
        proxy_path.write_bytes(b"proxy-image")
        photo_id = self.db.get_photo_by_path(os.path.join(self.folder, "a.jpg"))["id"]
        self.db.update_proxy_path(photo_id, str(proxy_path))
        resp = self.client.get(f"/api/proxy/{photo_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b"proxy-image")
        resp.close()

    def test_invalid_ids_return_400(self) -> None:
        self.assertEqual(
            self.client.post("/api/favorite", json={"id": "x"}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/reanalyze", json={"id": "x"}).status_code, 400
        )


if __name__ == "__main__":
    unittest.main()
