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
from photo_reviewer.cache import (
    cleanup_folder_cache,
    load_scan_results_from_cache,
)
from photo_reviewer.config import PERSISTED_FIELDS, Config
from photo_reviewer.db import Database, parse_capture_datetime, sort_photos
from photo_reviewer.importer import (
    FULL,
    RAW_POLICY_BOTH,
    RAW_POLICY_IMAGE_FIRST,
    RAW_POLICY_RAW_ONLY,
    ImportError_,
    plan_import,
    run_import,
)
from photo_reviewer.paths import canonical_path
from photo_reviewer.scanner import (
    JOBS,
    discover_images,
    discover_photo_pairs,
    start_scan,
)
from photo_reviewer.server import create_app
from photo_reviewer.thumbnailer import (
    cleanup_cache,
    hamming_distance,
    make_proxy,
)


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
        self.db.close()
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
        # Capture-time sorting and year filtering are pushed into SQL, so a
        # paginated page must be correct without loading the whole table.
        page = self.db.list_photos(folder=folder, sort="time_asc", limit=2, offset=0)
        self.assertEqual([p["filename"] for p in page], ["c.jpg", "b.jpg"])
        next_page = self.db.list_photos(
            folder=folder, sort="time_asc", limit=2, offset=2
        )
        self.assertEqual([p["filename"] for p in next_page], ["d.jpg", "a.jpg"])
        year_page = self.db.list_photos(folder=folder, year=2024, limit=10)
        self.assertEqual([p["filename"] for p in year_page], ["d.jpg"])
        year_missing = self.db.list_photos(folder=folder, year=1999, limit=10)
        self.assertEqual(year_missing, [])

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

    def test_restore_deleted_returns_pending_when_never_analyzed(self) -> None:
        folder = os.path.join(self.tmp.name, "pics")
        pending_path = os.path.join(folder, "pending.jpg")
        analyzed_path = os.path.join(folder, "analyzed.jpg")
        self.db.upsert_photo(
            {
                "path": pending_path,
                "folder": folder,
                "filename": "pending.jpg",
                "status": "pending",
            }
        )
        self.db.upsert_photo(
            {
                "path": analyzed_path,
                "folder": folder,
                "filename": "analyzed.jpg",
                "status": "analyzed",
                "score": 7.5,
                "analyzed_at": "2024-01-01T00:00:00",
            }
        )
        pending_id = self.db.get_photo_by_path(pending_path)["id"]
        analyzed_id = self.db.get_photo_by_path(analyzed_path)["id"]
        trash_p = os.path.join(folder, ".trash", "pending.jpg")
        trash_a = os.path.join(folder, ".trash", "analyzed.jpg")
        self.db.mark_deleted(
            [pending_id, analyzed_id],
            {pending_id: trash_p, analyzed_id: trash_a},
        )
        # Restoring both: pending stays pending, analyzed stays analyzed.
        self.db.restore_deleted(
            [pending_id, analyzed_id],
            {pending_id: pending_path, analyzed_id: analyzed_path},
        )
        self.assertEqual(
            self.db.get_photo(pending_id)["status"], "pending"
        )
        self.assertEqual(
            self.db.get_photo(analyzed_id)["status"], "analyzed"
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

    def test_thumbnail_quality_defaults_to_fifty(self) -> None:
        from photo_reviewer.config import Config

        cfg = Config(data_dir=self.tmp.name)
        self.assertEqual(cfg.thumb_quality, 50)

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

    def test_prompt_presets_are_style_only_and_format_appends_once(self) -> None:
        from photo_reviewer.api_client import (
            DEFAULT_STYLE_PROMPT,
            JSON_FORMAT_MARKER,
            PROMPT_PRESETS,
            SYSTEM_PROMPT,
            build_system_prompt,
            strip_json_format,
        )

        for name, template in PROMPT_PRESETS.items():
            self.assertNotIn(JSON_FORMAT_MARKER, template, name)
            self.assertIn(JSON_FORMAT_MARKER, build_system_prompt(template))

        # Presets only change the style line; the rest of the body stays identical.
        rendered = [template.splitlines() for template in PROMPT_PRESETS.values()]
        body = [line for line in rendered[0][1:] if line.strip()]
        for template_lines in rendered[1:]:
            other_body = [line for line in template_lines[1:] if line.strip()]
            self.assertEqual(other_body, body)

        # Old/full prompts must not duplicate the shared JSON format block.
        built = build_system_prompt(SYSTEM_PROMPT)
        self.assertEqual(built.count(JSON_FORMAT_MARKER), 1)
        self.assertTrue(built.startswith(DEFAULT_STYLE_PROMPT))
        self.assertTrue(built.rstrip().endswith("}"))
        self.assertEqual(strip_json_format(SYSTEM_PROMPT), DEFAULT_STYLE_PROMPT)

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
        self.db.close()
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

        def slow_prepare(image_path, folder, config, raw_path=None):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.08)
            try:
                return original_prepare_proxy(image_path, folder, config, raw_path)
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

    def test_rebuild_attaches_a_late_raw_sibling(self) -> None:
        """A .nef copied in later must light up the badge on an indexed .jpg.

        The jpg is already known, so the scan skips re-indexing it; without an
        explicit attach step the pair would look identical on disk while the
        card kept showing no RAW marker.
        """
        image_path = os.path.join(self.folder, "late.jpg")
        Image.new("RGB", (40, 40)).save(image_path)
        self.db.upsert_photo(
            {
                "path": image_path,
                "folder": self.folder,
                "filename": "late.jpg",
                "status": "analyzed",
                "score": 6.5,
            }
        )
        raw_path = os.path.join(self.folder, "late.NEF")
        Path(raw_path).write_bytes(b"raw-bytes")
        from photo_reviewer.scanner import start_rebuild_index

        job = start_rebuild_index(self.folder, self.db, self.cfg)
        wait_for_job(job.id)

        row = self.db.get_photo_by_path(image_path)
        self.assertEqual(row["raw_path"], raw_path)
        self.assertEqual(row["score"], 6.5, "attaching the raw must not touch scores")
        # The stub that a Mac would leave behind is ignored as well.
        Path(os.path.join(self.folder, "._late.NEF")).write_bytes(b"stub")
        job = start_rebuild_index(self.folder, self.db, self.cfg)
        wait_for_job(job.id)
        self.assertEqual(self.db.get_photo_by_path(image_path)["raw_path"], raw_path)
        self.assertIsNone(
            self.db.get_photo_by_path(os.path.join(self.folder, "._late.NEF"))
        )

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

    def test_geocoding_runs_once_and_survives_later_passes(self) -> None:
        """The analysed pass must not repeat the index pass's network call.

        EXIF is re-read from the file on every pass, so the "already resolved"
        flag has to live in the database; otherwise every photo with GPS is
        geocoded twice per scan (and again on every re-analysis).
        """
        image_path = os.path.join(self.folder, "gps.jpg")
        img = Image.new("RGB", (40, 40))
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (30.0, 15.0, 0.0), 3: "E", 4: (120.0, 10.0, 0.0)}
        img.save(image_path, exif=exif)
        calls = []

        def fake_geocode(lat, lon, provider, api_key, interval=1.0, retries=3):
            calls.append((lat, lon))
            return "杭州西湖"

        with (
            patch("photo_reviewer.scanner.reverse_geocode", side_effect=fake_geocode),
            patch("photo_reviewer.pipeline.reverse_geocode", side_effect=fake_geocode),
            patch("photo_reviewer.scanner.analyze_prepared", side_effect=fake_analysis),
        ):
            wait_for_job(start_scan(self.folder, self.db, self.cfg).id)
            self.assertEqual(len(calls), 1, "index + analyse must share one lookup")
            photo_id = self.db.get_photo_by_path(image_path)["id"]
            row = self.db.get_photo(photo_id)
            self.assertEqual(row["location"], "杭州西湖")
            self.assertTrue(row["geocoded"])

            # A forced re-scan refreshes EXIF but keeps the resolved address.
            wait_for_job(start_scan(self.folder, self.db, self.cfg, force=True).id)
            self.assertEqual(len(calls), 1, "a re-scan must not re-geocode")

            from photo_reviewer.scanner import reanalyze_photo

            updated = reanalyze_photo(photo_id, self.db, self.cfg)
            self.assertEqual(len(calls), 1, "re-analysis must not re-geocode")
            self.assertEqual(updated["location"], "杭州西湖")
            self.assertTrue(updated["geocoded"])

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
        self.db.close()
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
        from photo_reviewer.api_client import JSON_FORMAT_MARKER

        data = self.client.get("/api/prompt-presets").get_json()
        self.assertEqual(data["default"], "playful")
        for key in ("playful", "literary", "melancholy", "humorous", "warm"):
            self.assertTrue(data["presets"][key].strip())
            self.assertNotIn(JSON_FORMAT_MARKER, data["presets"][key])

    def test_config_save_strips_prompt_format_suffix(self) -> None:
        from photo_reviewer.api_client import JSON_FORMAT_MARKER, SYSTEM_PROMPT

        resp = self.client.post("/api/config", json={"system_prompt": SYSTEM_PROMPT})
        self.assertEqual(resp.status_code, 200)
        cfg = self.client.get("/api/config").get_json()
        self.assertNotIn(JSON_FORMAT_MARKER, cfg["system_prompt"])

    def test_model_queue_and_retry_settings_persist(self) -> None:
        resp = self.client.post(
            "/api/config",
            json={
                "scan_concurrency": 2,
                "model_retries": 4,
                "gallery_thumb_size": 320,
                "proxy_quality": 75,
                "thumb_quality": 45,
            },
        )
        self.assertEqual(resp.status_code, 200)
        cfg = self.client.get("/api/config").get_json()
        self.assertEqual(cfg["scan_concurrency"], 2)
        self.assertEqual(cfg["model_retries"], 4)
        self.assertEqual(cfg["gallery_thumb_size"], 320)
        self.assertEqual(cfg["proxy_quality"], 75)
        self.assertEqual(cfg["thumb_quality"], 45)

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

    def test_bad_pagination_returns_json_400(self) -> None:
        """Numeric query parameters are validated instead of raising a 500."""
        for query in ("limit=abc", "offset=abc", "limit=", "limit=1e3"):
            resp = self.client.get(f"/api/photos?{query}")
            self.assertEqual(resp.status_code, 400, query)
            self.assertEqual(resp.get_json()["error"], "limit/offset 参数不合法")

    def test_negative_limit_is_clamped(self) -> None:
        resp = self.client.get("/api/photos?limit=-5")
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(resp.get_json()["photos"]), 1)

    def test_cross_origin_write_is_rejected(self) -> None:
        with self.assertLogs("photo_reviewer.server", level="WARNING"):
            resp = self.client.post(
                "/api/favorite",
                json={"id": 1, "favorite": True},
                headers={"Origin": "http://evil.example"},
            )
        self.assertEqual(resp.status_code, 403)

    def test_same_origin_write_is_allowed(self) -> None:
        resp = self.client.post(
            "/api/favorite",
            json={"id": 1, "favorite": True},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["favorite"])

    def test_csrf_style_content_type_is_still_origin_checked(self) -> None:
        """A text/plain body is parsed by Flask, so the Origin guard must hold."""
        with self.assertLogs("photo_reviewer.server", level="WARNING"):
            resp = self.client.post(
                "/api/delete",
                data=json.dumps({"ids": [1]}),
                content_type="text/plain",
                headers={"Origin": "http://evil.example"},
            )
        self.assertEqual(resp.status_code, 403)

    def test_invalid_config_payload_changes_nothing(self) -> None:
        """A rejected field must not leave the running config half-updated."""
        before = self.cfg.model_retries
        resp = self.client.post(
            "/api/config", json={"model_retries": 7, "request_timeout": "nope"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.cfg.model_retries, before)

    def test_non_editable_settings_keys_are_ignored(self) -> None:
        resp = self.client.post(
            "/api/config", json={"cache_dir_name": "..", "host": "0.0.0.0"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.cfg.cache_dir_name, ".photo-review-cache")
        self.assertNotEqual(self.cfg.host, "0.0.0.0")

    def test_host_port_debug_are_not_persisted(self) -> None:
        self.client.post("/api/config", json={"model": "next-model"})
        stored = self.db.get_settings()
        for key in ("host", "port", "debug"):
            self.assertNotIn(key, stored)
        self.assertNotIn("host", PERSISTED_FIELDS)
        self.assertIn("model", stored)

    def test_photo_payload_is_trimmed_for_the_gallery(self) -> None:
        resp = self.client.get("/api/photos")
        photo = resp.get_json()["photos"][0]
        self.assertIn("path", photo)
        self.assertIn("raw_path", photo)
        self.assertIn("is_raw", photo)
        self.assertNotIn("phash", photo)
        self.assertNotIn("proxy_path", photo)

    def test_list_payload_carries_the_dimension_scores(self) -> None:
        """The detail panel and the cards read dimensions from the list call.

        Dropping the field made every sub-score render as 0.0 even though the
        total was correct, because the gallery never re-fetches the photo.
        """
        photo_id = self.db.get_photo_by_path(
            os.path.join(self.folder, "a.jpg")
        )["id"]
        self.db.update_metadata(
            photo_id,
            8.2,
            {"technical": 7.5, "composition": 8.5, "memory": 9.0, "uniqueness": 7.8},
            ["夏日"],
            "很好",
            "标题",
            "杭州",
        )
        listing = self.client.get("/api/photos").get_json()["photos"][0]
        self.assertEqual(listing["dimensions"]["technical"], 7.5)
        self.assertEqual(listing["dimensions"]["memory"], 9.0)
        detail = self.client.get(f"/api/photo/{photo_id}").get_json()
        self.assertEqual(detail["dimensions"], listing["dimensions"])

    def test_trash_roundtrip_carries_the_raw_sibling(self) -> None:
        """A raw file must follow its photo into and out of the trash."""
        source = os.path.join(self.folder, "a.jpg")
        raw = os.path.join(self.folder, "a.NEF")
        Image.new("RGB", (16, 16)).save(source)
        Path(raw).write_bytes(b"raw-bytes")
        self.db.upsert_photo(
            {
                "path": source,
                "folder": self.folder,
                "filename": "a.jpg",
                "status": "analyzed",
                "raw_path": raw,
            }
        )
        photo_id = self.db.get_photo_by_path(source)["id"]

        resp = self.client.post("/api/delete", json={"ids": [photo_id]})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(source))
        self.assertFalse(os.path.exists(raw), "raw sibling stayed in the album")
        trashed = self.db.get_photo(photo_id)
        day_dir = os.path.dirname(trashed["path"])
        self.assertTrue(os.path.exists(os.path.join(day_dir, "a.NEF")))
        self.assertIsNone(trashed["raw_path"])

        resp = self.client.post("/api/restore", json={"ids": [photo_id]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.exists(source))
        self.assertTrue(os.path.exists(raw), "raw sibling was not restored")
        self.assertEqual(self.db.get_photo(photo_id)["raw_path"], raw)

    def test_trash_empty_can_delete_raw_files(self) -> None:
        source = os.path.join(self.folder, "b.jpg")
        raw = os.path.join(self.folder, "b.NEF")
        Image.new("RGB", (16, 16)).save(source)
        Path(raw).write_bytes(b"raw-bytes")
        self.db.upsert_photo(
            {
                "path": source,
                "folder": self.folder,
                "filename": "b.jpg",
                "status": "analyzed",
                "raw_path": raw,
            }
        )
        photo_id = self.db.get_photo_by_path(source)["id"]
        self.client.post("/api/delete", json={"ids": [photo_id]})
        trashed_image = self.db.get_photo(photo_id)["path"]
        trashed_raw = os.path.join(os.path.dirname(trashed_image), "b.NEF")
        self.assertTrue(os.path.exists(trashed_raw))

        resp = self.client.post("/api/trash/empty", json={"delete_raw": True})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(body["raw_deleted"], 1)
        self.assertFalse(os.path.exists(trashed_image))
        self.assertFalse(os.path.exists(trashed_raw))
        self.assertIsNone(self.db.get_photo(photo_id))

    def test_trash_empty_can_keep_raw_files(self) -> None:
        source = os.path.join(self.folder, "c.jpg")
        raw = os.path.join(self.folder, "c.NEF")
        Image.new("RGB", (16, 16)).save(source)
        Path(raw).write_bytes(b"raw-bytes")
        self.db.upsert_photo(
            {
                "path": source,
                "folder": self.folder,
                "filename": "c.jpg",
                "status": "analyzed",
                "raw_path": raw,
            }
        )
        photo_id = self.db.get_photo_by_path(source)["id"]
        self.client.post("/api/delete", json={"ids": [photo_id]})
        trashed_image = self.db.get_photo(photo_id)["path"]
        trashed_raw = os.path.join(os.path.dirname(trashed_image), "c.NEF")

        resp = self.client.post("/api/trash/empty", json={"delete_raw": False})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(body["raw_deleted"], 0)
        self.assertFalse(os.path.exists(trashed_image))
        self.assertTrue(os.path.exists(trashed_raw), "raw should have been kept")

    def test_delete_restore_roundtrip_moves_the_file(self) -> None:
        source = os.path.join(self.folder, "a.jpg")
        Image.new("RGB", (16, 16)).save(source)
        photo_id = self.db.get_photo_by_path(source)["id"]

        resp = self.client.post("/api/delete", json={"ids": [photo_id]})
        self.assertEqual(resp.status_code, 200)
        moved = resp.get_json()["moved"]
        self.assertEqual(len(moved), 1)
        self.assertFalse(os.path.exists(source))
        self.assertTrue(os.path.exists(moved[0]["path"]))

        resp = self.client.post("/api/restore", json={"ids": [photo_id]})
        self.assertEqual(resp.status_code, 200)
        restored = resp.get_json()["restored"]
        self.assertEqual(len(restored), 1)
        self.assertTrue(os.path.exists(restored[0]["path"]))
        self.assertEqual(
            self.db.get_photo(photo_id)["status"], "pending"
        )

    def test_permanent_delete_removes_file_and_row(self) -> None:
        source = os.path.join(self.folder, "a.jpg")
        Image.new("RGB", (16, 16)).save(source)
        photo_id = self.db.get_photo_by_path(source)["id"]
        self.client.post("/api/delete", json={"ids": [photo_id]})
        trashed = self.db.get_photo(photo_id)["path"]

        resp = self.client.post("/api/delete/permanent", json={"ids": [photo_id]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["purged"][0]["id"], photo_id)
        self.assertFalse(os.path.exists(trashed))
        self.assertIsNone(self.db.get_photo(photo_id))

    def test_permanent_delete_removes_the_trashed_raw_too(self) -> None:
        source = os.path.join(self.folder, "d.jpg")
        raw = os.path.join(self.folder, "d.NEF")
        Image.new("RGB", (16, 16)).save(source)
        Path(raw).write_bytes(b"raw-bytes")
        self.db.upsert_photo(
            {
                "path": source,
                "folder": self.folder,
                "filename": "d.jpg",
                "status": "analyzed",
                "raw_path": raw,
            }
        )
        photo_id = self.db.get_photo_by_path(source)["id"]
        self.client.post("/api/delete", json={"ids": [photo_id]})
        trashed_image = self.db.get_photo(photo_id)["path"]
        trashed_raw = os.path.join(os.path.dirname(trashed_image), "d.NEF")
        self.assertTrue(os.path.exists(trashed_raw))

        resp = self.client.post("/api/delete/permanent", json={"ids": [photo_id]})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(trashed_image))
        self.assertFalse(os.path.exists(trashed_raw), "orphan raw left in the trash")
        self.assertIsNone(self.db.get_photo(photo_id))

    def test_thumbnail_prefers_the_sibling_thumb_file(self) -> None:
        """A row pointing at the proxy still serves the small thumbnail."""
        source = os.path.join(self.folder, "a.jpg")
        Image.new("RGB", (64, 48), (200, 120, 60)).save(source)
        photo_id = self.db.get_photo_by_path(source)["id"]
        resp = self.client.get(f"/api/thumbnail/{photo_id}")
        resp.close()
        photo = self.db.get_photo(photo_id)
        thumb = photo["thumb_path"]
        self.assertTrue(thumb.endswith("_thumb.jpg"))
        self.assertTrue(os.path.exists(thumb))

        # Simulate a database written by an older version.
        self.db.update_proxy_path(photo_id, photo["proxy_path"])
        import sqlite3

        conn = sqlite3.connect(self.cfg.db_path)
        conn.execute(
            "UPDATE photos SET thumb_path=? WHERE id=?",
            (photo["proxy_path"], photo_id),
        )
        conn.commit()
        conn.close()

        resp = self.client.get(f"/api/thumbnail/{photo_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data()[:2], b"\xff\xd8")
        resp.close()


class RawSupportTests(unittest.TestCase):
    """Raw files are second-class citizens: the JPEG is the analysed photo."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _write(self, name: str, size=(32, 24)) -> str:
        path = os.path.join(self.folder, name)
        Image.new("RGB", size, (120, 160, 200)).save(path)
        return path

    def test_pairs_prefer_the_image_and_remember_the_raw(self) -> None:
        jpg = self._write("DSC_0001.JPG")
        raw = os.path.join(self.folder, "DSC_0001.NEF")
        Path(raw).write_bytes(b"raw-payload")
        self._write("lonely.jpg")

        pairs = dict(discover_photo_pairs(self.folder))
        self.assertIn(jpg, pairs)
        self.assertEqual(pairs[jpg], raw)
        self.assertEqual(len(pairs), 2)
        self.assertNotIn(raw, pairs)

    def test_raw_only_files_are_indexed_on_their_own(self) -> None:
        raw = os.path.join(self.folder, "lonely.ARW")
        Path(raw).write_bytes(b"raw-payload")
        pairs = discover_photo_pairs(self.folder)
        self.assertEqual(pairs, [(raw, None)])
        self.assertEqual(discover_images(self.folder), [raw])

    def test_raw_matching_is_case_insensitive_on_the_extension(self) -> None:
        jpg = self._write("IMG_1.jpg")
        raw = os.path.join(self.folder, "IMG_1.nef")
        Path(raw).write_bytes(b"raw")
        pairs = dict(discover_photo_pairs(self.folder))
        self.assertEqual(pairs[jpg], raw)
        self.assertEqual(len(pairs), 1)

    def test_scan_records_raw_path_and_marks_the_photo(self) -> None:
        self._write("DSC_0002.JPG")
        raw = os.path.join(self.folder, "DSC_0002.NEF")
        Path(raw).write_bytes(b"raw")
        raw_only = os.path.join(self.folder, "SOLO.ARW")
        Path(raw_only).write_bytes(b"raw")

        with patch(
            "photo_reviewer.scanner.analyze_prepared", side_effect=fake_analysis
        ):
            job = start_scan(self.folder, self.db, self.cfg)
            result = wait_for_job(job.id)

        self.assertEqual(result.status, "completed")
        rows = {p["filename"]: p for p in self.db.list_photos(folder=self.folder)}
        self.assertEqual(set(rows), {"DSC_0002.JPG", "SOLO.ARW"})
        self.assertEqual(rows["DSC_0002.JPG"]["raw_path"], raw)
        self.assertIsNone(rows["SOLO.ARW"]["raw_path"])

    def test_gallery_payload_flags_raw_siblings(self) -> None:
        jpg = self._write("DSC_0003.JPG")
        raw = os.path.join(self.folder, "DSC_0003.NEF")
        Path(raw).write_bytes(b"raw")
        self.db.upsert_photo(
            {
                "path": jpg,
                "folder": self.folder,
                "filename": "DSC_0003.JPG",
                "status": "analyzed",
                "score": 8.0,
                "raw_path": raw,
            }
        )
        app = create_app(self.cfg, self.db)
        client = app.test_client()
        photo = client.get("/api/photos").get_json()["photos"][0]
        self.assertTrue(photo["has_raw"])
        self.assertFalse(photo["is_raw"])
        self.assertEqual(photo["raw_ext"], "NEF")

    def test_raw_only_photo_is_flagged_as_raw(self) -> None:
        raw = os.path.join(self.folder, "SOLO.ARW")
        Path(raw).write_bytes(b"raw")
        self.db.upsert_photo(
            {
                "path": raw,
                "folder": self.folder,
                "filename": "SOLO.ARW",
                "status": "analyzed",
                "score": 6.0,
            }
        )
        app = create_app(self.cfg, self.db)
        client = app.test_client()
        photo = client.get("/api/photos").get_json()["photos"][0]
        self.assertTrue(photo["is_raw"])
        self.assertFalse(photo["has_raw"])

    def test_embedded_preview_is_extracted_from_a_fake_raw(self) -> None:
        """A TIFF container holding a JPEG preview is rendered from the preview."""
        import io
        import random

        rng = random.Random(7)
        noisy = Image.new("RGB", (400, 300))
        noisy.putdata(
            [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(400 * 300)]
        )
        buffer = io.BytesIO()
        noisy.save(buffer, "JPEG", quality=95)
        preview = buffer.getvalue()
        self.assertGreater(len(preview), 5000)

        raw_path = os.path.join(self.folder, "embedded.nef")
        with open(raw_path, "wb") as fh:
            fh.write(b"II*\x00\x08\x00\x00\x00")  # TIFF header
            fh.write(b"\x00" * 64)
            fh.write(preview)
            fh.write(b"\x00" * 32)

        proxy, width, height, image_hash, thumb = make_proxy(
            raw_path, cache_dir=os.path.join(self.tmp.name, "cache")
        )
        self.assertEqual((width, height), (400, 300))
        self.assertTrue(proxy.endswith("_proxy.jpg"))
        self.assertTrue(thumb.endswith("_thumb.jpg"))
        self.assertEqual(len(image_hash), 16)
        with Image.open(thumb) as img:
            self.assertEqual(img.size, (400, 300))

    def test_thumbnailer_falls_back_when_no_preview_exists(self) -> None:
        """A raw file with no embedded JPEG still goes through Pillow."""
        staged = os.path.join(self.folder, "plain.jpg")
        Image.new("RGB", (48, 36), (30, 90, 150)).save(staged, "TIFF")
        raw_path = os.path.join(self.folder, "plain.dng")
        os.replace(staged, raw_path)
        proxy, width, height, _hash, thumb = make_proxy(
            raw_path, cache_dir=os.path.join(self.tmp.name, "cache")
        )
        self.assertEqual((width, height), (48, 36))
        self.assertTrue(os.path.exists(proxy))
        self.assertTrue(os.path.exists(thumb))


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.db = Database(os.path.join(self.tmp.name, "library.db"))

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_cleanup_removes_only_unreferenced_files(self) -> None:
        cache = os.path.join(self.folder, ".cache")
        os.makedirs(cache)
        keep = os.path.join(cache, "keep_proxy.jpg")
        drop = os.path.join(cache, "drop_proxy.jpg")
        for path in (keep, drop):
            Path(path).write_bytes(b"x" * 10)
        self.db.upsert_photo(
            {
                "path": os.path.join(self.folder, "a.jpg"),
                "folder": self.folder,
                "filename": "a.jpg",
                "proxy_path": keep,
                "thumb_path": keep,
                "status": "analyzed",
            }
        )
        result = cleanup_folder_cache(self.folder, self.db, ".cache")
        self.assertEqual(result["freed"], 1)
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(drop))

    def test_cleanup_sweeps_temporary_files(self) -> None:
        cache = os.path.join(self.folder, ".cache")
        os.makedirs(cache)
        leftover = os.path.join(cache, "half_proxy.jpg.1234.5678.tmp")
        Path(leftover).write_bytes(b"partial")
        cleanup_folder_cache(self.folder, self.db, ".cache")
        self.assertFalse(os.path.exists(leftover))

    def test_cleanup_cache_reports_affected_photos(self) -> None:
        cache = os.path.join(self.tmp.name, "cache")
        os.makedirs(cache)
        Path(os.path.join(cache, "abc_proxy.jpg")).write_bytes(b"x")
        Path(os.path.join(cache, "abc_thumb.jpg")).write_bytes(b"x")
        Path(os.path.join(cache, "notes.txt")).write_bytes(b"keep me")
        result = cleanup_cache([], cache)
        self.assertEqual(result["freed"], 2)
        self.assertEqual(result["photos_affected"], 1)
        self.assertTrue(os.path.exists(os.path.join(cache, "notes.txt")))

    def test_legacy_cache_import_skips_deleted_and_analyzed(self) -> None:
        cache = os.path.join(self.folder, ".cache")
        os.makedirs(cache)
        payload = {
            "photos": [
                {"path": os.path.join(self.folder, "new.jpg"), "status": "analyzed"},
                {"path": os.path.join(self.folder, "gone.jpg"), "status": "deleted"},
                {"path": os.path.join(self.folder, "done.jpg"), "status": "analyzed"},
            ]
        }
        cache_file = os.path.join(cache, "scan_results.json")
        Path(cache_file).write_text(json.dumps(payload), encoding="utf-8")
        self.db.upsert_photo(
            {
                "path": os.path.join(self.folder, "gone.jpg"),
                "folder": self.folder,
                "filename": "gone.jpg",
                "status": "deleted",
            }
        )
        self.db.upsert_photo(
            {
                "path": os.path.join(self.folder, "done.jpg"),
                "folder": self.folder,
                "filename": "done.jpg",
                "status": "analyzed",
                "score": 9.0,
            }
        )

        imported = load_scan_results_from_cache(self.folder, self.db, ".cache")
        self.assertEqual(imported, 1)
        self.assertIsNotNone(
            self.db.get_photo_by_path(os.path.join(self.folder, "new.jpg"))
        )
        self.assertEqual(
            self.db.get_photo_by_path(os.path.join(self.folder, "done.jpg"))["score"],
            9.0,
        )
        self.assertFalse(os.path.exists(cache_file))


class PathAndSortTests(unittest.TestCase):
    def test_canonical_path_normalises_case(self) -> None:
        here = os.path.abspath(".")
        self.assertEqual(canonical_path(here.lower()), canonical_path(here.upper()))

    def test_sort_photos_supports_every_ui_sort_key(self) -> None:
        photos = [
            {"filename": "b.jpg", "score": 5.0, "size": 10, "exif": {}, "analyzed_at": "2024-01-01"},
            {"filename": "a.jpg", "score": 9.0, "size": 30, "exif": {}, "analyzed_at": "2023-01-01"},
            {"filename": "c.jpg", "score": None, "size": 20, "exif": {}, "analyzed_at": None},
        ]
        self.assertEqual(
            [p["filename"] for p in sort_photos(list(photos), "score_desc")],
            ["a.jpg", "b.jpg", "c.jpg"],
        )
        self.assertEqual(
            [p["filename"] for p in sort_photos(list(photos), "score_asc")],
            ["b.jpg", "a.jpg", "c.jpg"],
        )
        self.assertEqual(
            [p["filename"] for p in sort_photos(list(photos), "filename")],
            ["a.jpg", "b.jpg", "c.jpg"],
        )
        self.assertEqual(
            [p["filename"] for p in sort_photos(list(photos), "size")],
            ["a.jpg", "c.jpg", "b.jpg"],
        )
        self.assertEqual(
            [p["filename"] for p in sort_photos(list(photos), "recent")],
            ["b.jpg", "a.jpg", "c.jpg"],
        )

    def test_sort_photos_orders_capture_time_with_missing_last(self) -> None:
        photos = [
            {"filename": "old.jpg", "exif": {"datetime_original": "2019:01:01 00:00:00"}},
            {"filename": "none.jpg", "exif": {}},
            {"filename": "new.jpg", "exif": {"datetime_original": "2024:01:01 00:00:00"}},
        ]
        asc = [p["filename"] for p in sort_photos(list(photos), "time_asc")]
        desc = [p["filename"] for p in sort_photos(list(photos), "time_desc")]
        self.assertEqual(asc, ["old.jpg", "new.jpg", "none.jpg"])
        self.assertEqual(desc, ["new.jpg", "old.jpg", "none.jpg"])

    def test_hamming_distance_rejects_mixed_width_hashes(self) -> None:
        self.assertEqual(hamming_distance("00", "0f"), 4)
        self.assertEqual(hamming_distance("00", "000f"), 999)
        self.assertEqual(hamming_distance("", "0f"), 999)
        self.assertEqual(hamming_distance("zz", "0f"), 999)
        self.assertEqual(hamming_distance("f" * 16, "0" * 16), 64)


class DiscoverImagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_folder_raises(self) -> None:
        with self.assertRaises(NotADirectoryError):
            discover_images(os.path.join(self.tmp.name, "nope"))

    def test_skips_trash_and_git_directories(self) -> None:
        Path(os.path.join(self.folder, "keep.jpg")).write_bytes(b"x")
        for name in (".photo-trash", ".git", "__pycache__"):
            nested = os.path.join(self.folder, name)
            os.makedirs(nested)
            Path(os.path.join(nested, "hidden.jpg")).write_bytes(b"x")
        found = discover_images(self.folder, skip_dirs=[".photo-trash"])
        self.assertEqual([os.path.basename(p) for p in found], ["keep.jpg"])

    def test_results_are_sorted_and_filtered_by_extension(self) -> None:
        for name in ("b.jpg", "a.png", "c.txt", "d.webp"):
            Path(os.path.join(self.folder, name)).write_bytes(b"x")
        found = [os.path.basename(p) for p in discover_images(self.folder)]
        self.assertEqual(found, ["a.png", "b.jpg", "d.webp"])

    def test_macos_resource_forks_are_not_indexed(self) -> None:
        """``._DSC_0001.NEF`` is AppleDouble metadata, not a photograph.

        A card read on a Mac carries these 4 KB stubs next to the real files.
        They share the stem and the extension of the photo, so indexing them
        would shadow the picture or appear as a corrupt raw.
        """
        real_raw = os.path.join(self.folder, "DSC_0001.NEF")
        Path(real_raw).write_bytes(b"raw-bytes")
        Path(os.path.join(self.folder, "._DSC_0001.NEF")).write_bytes(b"\x00\x05\x16\x07")
        Path(os.path.join(self.folder, "._DSC_0002.JPG")).write_bytes(b"\x00\x05\x16\x07")
        Path(os.path.join(self.folder, "DSC_0002.JPG")).write_bytes(b"jpeg")

        pairs = discover_photo_pairs(self.folder)
        names = sorted(os.path.basename(image) for image, _raw in pairs)
        self.assertEqual(names, ["DSC_0001.NEF", "DSC_0002.JPG"])
        # The real file keeps its own pairing and is not shadowed by the stub.
        by_name = {os.path.basename(image): raw for image, raw in pairs}
        self.assertIsNone(by_name["DSC_0001.NEF"])
        self.assertIsNone(by_name["DSC_0002.JPG"])

    def test_metadata_stub_does_not_shadow_a_pair(self) -> None:
        image = os.path.join(self.folder, "DSC_0003.JPG")
        raw = os.path.join(self.folder, "DSC_0003.NEF")
        Path(image).write_bytes(b"jpeg")
        Path(raw).write_bytes(b"raw")
        Path(os.path.join(self.folder, "._DSC_0003.NEF")).write_bytes(b"stub")
        pairs = discover_photo_pairs(self.folder)
        self.assertEqual(pairs, [(image, raw)])


class ImportPlanTests(unittest.TestCase):
    """The SD-card import: it must never touch what the card does not have."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.card = os.path.join(self.tmp.name, "card")
        self.local = os.path.join(self.tmp.name, "local")
        os.makedirs(os.path.join(self.card, "DCIM", "100D3500"))
        os.makedirs(os.path.join(self.card, "DCIM", "101D3500"))
        os.makedirs(self.local)
        self.cfg = Config(data_dir=os.path.join(self.tmp.name, "data"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _image(self, root, *parts, color=(120, 90, 60), size=(24, 18)) -> str:
        path = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Image.new("RGB", size, color).save(path, "JPEG")
        return path

    def _raw(self, root, *parts, payload=b"raw-payload") -> str:
        path = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_bytes(payload)
        return path

    def _seed_card(self) -> None:
        self._image(self.card, "DCIM", "100D3500", "DSC_0001.JPG")
        self._raw(self.card, "DCIM", "100D3500", "DSC_0001.NEF")
        self._image(self.card, "DCIM", "100D3500", "DSC_0002.JPG")
        self._raw(self.card, "DCIM", "101D3500", "DSC_0003.NEF")

    def test_incremental_copies_only_what_is_missing(self) -> None:
        self._seed_card()
        # The local folder already has DSC_0001 and one photo the card lacks.
        self._image(self.local, "DCIM", "100D3500", "DSC_0001.JPG", color=(1, 1, 1))
        keep = self._image(self.local, "DCIM", "100D3500", "KEEP.JPG", color=(2, 2, 2))

        plan = plan_import(self.card, self.local, self.cfg)
        relative = sorted(c.relative_path for c in plan.copies)
        self.assertEqual(
            relative,
            [
                os.path.join("DCIM", "100D3500", "DSC_0002.JPG"),
                os.path.join("DCIM", "101D3500", "DSC_0003.NEF"),
            ],
        )
        self.assertEqual(plan.skipped, 1)
        # Only KEEP.JPG is local-only: DSC_0001.JPG exists on both sides, it
        # simply differs, so it is counted as "already present" not "local only".
        self.assertEqual(plan.local_only, 1)
        self.assertEqual(plan.unpaired_raw, 1)

        report = run_import(plan)
        self.assertEqual(report["copied"], 2)
        self.assertTrue(os.path.exists(keep))
        self.assertTrue(
            os.path.exists(os.path.join(self.local, "DCIM", "101D3500", "DSC_0003.NEF"))
        )

    def test_repeat_import_is_a_no_op(self) -> None:
        self._seed_card()
        run_import(plan_import(self.card, self.local, self.cfg))
        again = plan_import(self.card, self.local, self.cfg)
        self.assertEqual(again.copies, [])
        self.assertGreater(again.skipped, 0)

    def test_raw_policy_controls_the_group(self) -> None:
        self._seed_card()
        self._image(self.local, "DCIM", "100D3500", "DSC_0001.JPG", color=(1, 1, 1))
        # image-first: the pair's jpg already exists, so its 22 MB raw is not
        # dragged along; the lone raw (no jpg on the card) still comes over.
        image_first = plan_import(
            self.card, self.local, self.cfg, raw_policy=RAW_POLICY_IMAGE_FIRST
        )
        copied = sorted(os.path.basename(c.relative_path) for c in image_first.copies)
        self.assertEqual(copied, ["DSC_0002.JPG", "DSC_0003.NEF"])

        both = plan_import(self.card, self.local, self.cfg, raw_policy=RAW_POLICY_BOTH)
        both_names = sorted(os.path.basename(c.relative_path) for c in both.copies)
        self.assertEqual(
            both_names, ["DSC_0001.NEF", "DSC_0002.JPG", "DSC_0003.NEF"]
        )

        raw_only = plan_import(
            self.card, self.local, self.cfg, raw_policy=RAW_POLICY_RAW_ONLY
        )
        raw_names = sorted(os.path.basename(c.relative_path) for c in raw_only.copies)
        # DSC_0002 has no raw on the card, so its jpg is the only version and
        # is still taken rather than silently dropping the photo.
        self.assertEqual(
            raw_names, ["DSC_0001.NEF", "DSC_0002.JPG", "DSC_0003.NEF"]
        )

    def test_raw_sibling_is_imported_even_when_the_jpg_exists(self) -> None:
        """A .nef that joins an already-imported .jpg must still be copied."""
        self._seed_card()
        self._image(self.local, "DCIM", "100D3500", "DSC_0001.JPG", color=(1, 1, 1))
        self._image(self.local, "DCIM", "100D3500", "DSC_0002.JPG")
        both = plan_import(self.card, self.local, self.cfg, raw_policy=RAW_POLICY_BOTH)
        names = sorted(os.path.basename(c.relative_path) for c in both.copies)
        self.assertIn("DSC_0001.NEF", names)
        # The jpg is byte-different but incremental mode leaves it alone.
        self.assertNotIn("DSC_0001.JPG", names)

    def test_full_mode_overwrites_only_changed_files(self) -> None:
        self._seed_card()
        # Same size and mtime as the card => considered identical.
        self._image(self.local, "DCIM", "100D3500", "DSC_0002.JPG")
        card_img = os.path.join(self.card, "DCIM", "100D3500", "DSC_0002.JPG")
        local_img = os.path.join(self.local, "DCIM", "100D3500", "DSC_0002.JPG")
        stat = os.stat(card_img)
        os.utime(local_img, (stat.st_atime, stat.st_mtime))

        # A different file under the same name => must be refreshed.
        stale = self._image(
            self.local, "DCIM", "100D3500", "DSC_0001.JPG", color=(9, 9, 9), size=(40, 30)
        )
        card_img_1 = os.path.join(self.card, "DCIM", "100D3500", "DSC_0001.JPG")
        self.assertNotEqual(os.path.getsize(stale), os.path.getsize(card_img_1))

        plan = plan_import(self.card, self.local, self.cfg, mode=FULL)
        overwritten = sorted(os.path.basename(c.relative_path) for c in plan.overwritten)
        self.assertEqual(overwritten, ["DSC_0001.JPG"])
        self.assertTrue(plan.overwritten[0].content_changed)
        run_import(plan)
        # The stale local copy is replaced by the card's version.
        self.assertEqual(os.path.getsize(stale), os.path.getsize(card_img_1))

    def test_local_only_files_survive_a_full_import(self) -> None:
        self._seed_card()
        keep = self._image(self.local, "DCIM", "100D3500", "ONLY_LOCAL.JPG")
        before = os.stat(keep)
        run_import(plan_import(self.card, self.local, self.cfg, mode=FULL))
        self.assertTrue(os.path.exists(keep))
        self.assertEqual(os.stat(keep).st_mtime, before.st_mtime)

    def test_rejects_target_inside_source(self) -> None:
        self._seed_card()
        nested = os.path.join(self.card, "DCIM", "100D3500")
        with self.assertRaises(ImportError_):
            plan_import(self.card, nested, self.cfg)

    def test_rejects_same_folder_and_missing_source(self) -> None:
        with self.assertRaises(ImportError_):
            plan_import(self.local, self.local, self.cfg)
        with self.assertRaises(ImportError_):
            plan_import(os.path.join(self.tmp.name, "nope"), self.local, self.cfg)

    def test_cancel_stops_at_a_file_boundary(self) -> None:
        self._seed_card()
        plan = plan_import(self.card, self.local, self.cfg)
        seen = {"count": 0}

        def should_cancel() -> bool:
            seen["count"] += 1
            return seen["count"] > 1

        report = run_import(plan, should_cancel=should_cancel)
        self.assertTrue(report["cancelled"])
        self.assertEqual(report["copied"], 1)


class ImportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.card = os.path.join(self.tmp.name, "card")
        self.local = os.path.join(self.tmp.name, "local")
        os.makedirs(os.path.join(self.card, "DCIM"))
        os.makedirs(self.local)
        Image.new("RGB", (24, 18), (120, 90, 60)).save(
            os.path.join(self.card, "DCIM", "DSC_0001.JPG"), "JPEG"
        )
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_plan_endpoint_reports_counts(self) -> None:
        resp = self.client.post(
            "/api/import/plan",
            json={"source": self.card, "target": self.local, "mode": "incremental"},
        )
        self.assertEqual(resp.status_code, 200)
        plan = resp.get_json()["plan"]
        self.assertEqual(plan["new_count"], 1)
        self.assertEqual(plan["overwrite_count"], 0)
        self.assertEqual(plan["local_only_count"], 0)

    def test_plan_endpoint_validates_input(self) -> None:
        resp = self.client.post("/api/import/plan", json={"target": self.local})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            "/api/import/plan",
            json={"source": self.card, "target": self.local, "mode": "nope"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_run_endpoint_copies_then_indexes(self) -> None:
        resp = self.client.post(
            "/api/import/run",
            json={
                "source": self.card,
                "target": self.local,
                "mode": "incremental",
                "after": "index",
            },
        )
        self.assertEqual(resp.status_code, 200)
        job_id = resp.get_json()["job_id"]
        deadline = time.time() + 15
        job = None
        while time.time() < deadline:
            job = JOBS.get(job_id)
            if job and job.status in ("completed", "cancelled", "error"):
                break
            time.sleep(0.05)
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "completed", job.error)
        copied = os.path.join(self.local, "DCIM", "DSC_0001.JPG")
        self.assertTrue(os.path.exists(copied))
        row = self.db.get_photo_by_path(copied)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")  # indexed, not analysed

    def test_copy_only_mode_skips_indexing(self) -> None:
        resp = self.client.post(
            "/api/import/run",
            json={
                "source": self.card,
                "target": self.local,
                "after": "copy",
            },
        )
        job_id = resp.get_json()["job_id"]
        deadline = time.time() + 15
        job = None
        while time.time() < deadline:
            job = JOBS.get(job_id)
            if job and job.status in ("completed", "cancelled", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(job.status, "completed")
        copied = os.path.join(self.local, "DCIM", "DSC_0001.JPG")
        self.assertTrue(os.path.exists(copied))
        self.assertIsNone(self.db.get_photo_by_path(copied))

    def test_open_file_rejects_a_foreign_path(self) -> None:
        photo = os.path.join(self.local, "DCIM", "DSC_0001.JPG")
        os.makedirs(os.path.dirname(photo), exist_ok=True)
        Image.new("RGB", (8, 8)).save(photo, "JPEG")
        self.db.upsert_photo(
            {
                "path": photo,
                "folder": os.path.dirname(photo),
                "filename": "DSC_0001.JPG",
                "status": "analyzed",
            }
        )
        photo_id = self.db.get_photo_by_path(photo)["id"]
        other = os.path.join(self.tmp.name, "secret.txt")
        Path(other).write_text("nope", encoding="utf-8")
        resp = self.client.post(
            "/api/open-file", json={"photo_id": photo_id, "path": other}
        )
        self.assertEqual(resp.status_code, 403)

    def test_open_file_requires_a_valid_photo(self) -> None:
        self.assertEqual(
            self.client.post("/api/open-file", json={"photo_id": "x"}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/open-file", json={"photo_id": 999}).status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
