"""Core regression tests for Lumina Album.

These tests run fully locally: all databases and image folders are temporary,
and model calls are replaced with deterministic fakes.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

from PIL import Image

from photo_reviewer.api_client import (
    DEFAULT_STYLE_PROMPT,
    JSON_FORMAT_SUFFIX,
    PROMPT_PRESETS,
    SemanticSearchError,
    _normalize_dimensions,
    _parse_model_json,
    analyze_image,
)
from photo_reviewer.cache import (
    cache_dir_for_folder,
    cleanup_folder_cache,
)
from photo_reviewer.cards import (
    KIND_DELETE,
    STATE_APPLIED,
    STATE_ERASE,
    STATE_PENDING,
    CardError,
    apply_pending,
    card_path_for,
    card_status,
    delete_from_card,
    find_on_card,
    pending_preview,
    purge_for_photo,
    record_pairing,
    relative_to_album,
    resolve_card,
    restore_to_card,
    volume_id,
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
from photo_reviewer.paths import canonical_path, is_in_named_dir
from photo_reviewer.repair import (
    needs_dimensions_repair,
    needs_orientation_repair,
    repair_dimension_keys,
    repair_orientation_sizes,
    run_startup_repairs,
)
from photo_reviewer.scanner import (
    JOBS,
    discover_images,
    discover_photo_pairs,
    start_scan,
)
from photo_reviewer.server import _remove_file, create_app
from photo_reviewer.thumbnailer import (
    cleanup_cache,
    hamming_distance,
    image_size_on_disk,
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
        "dimensions": {"technical": 7, "memory": 9, "uniqueness": 6},
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
            {"technical": 9, "memory": 7, "uniqueness": 6},
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
    def test_provider_none_never_calls_a_service(self) -> None:
        """The "不使用" option must keep every photo offline."""
        from unittest.mock import patch

        from photo_reviewer.geocode import reverse_geocode

        with patch("photo_reviewer.geocode._get_json") as get_json:
            result = reverse_geocode(
                30.25, 120.17, provider="none", api_key="k", interval=0.1
            )
        self.assertIsNone(result)
        get_json.assert_not_called()

    def test_geocoding_enabled_only_rejects_none(self) -> None:
        from photo_reviewer.geocode import geocoding_enabled

        self.assertTrue(geocoding_enabled("nominatim"))
        self.assertTrue(geocoding_enabled("amap"))
        self.assertTrue(geocoding_enabled(""))
        self.assertTrue(geocoding_enabled(None))
        self.assertFalse(geocoding_enabled("none"))
        self.assertFalse(geocoding_enabled("NONE"))
        self.assertFalse(geocoding_enabled(" none "))

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

    def test_late_image_absorbs_a_raw_only_row(self) -> None:
        """The mirror case: the jpg arrives after the raw was its own photo.

        Both halves now exist, so the album must show one card. The score the
        raw-only row had already earned moves to the image row instead of being
        thrown away, and the path must not leave a duplicate behind.
        """
        raw_path = os.path.join(self.folder, "pair.NEF")
        Path(raw_path).write_bytes(b"raw-bytes")
        self.db.upsert_photo(
            {
                "path": raw_path,
                "folder": self.folder,
                "filename": "pair.NEF",
                "status": "analyzed",
                "score": 7.7,
                "tags": ["来自RAW"],
                "title": "只有RAW时的标题",
            }
        )
        image_path = os.path.join(self.folder, "pair.JPG")
        Image.new("RGB", (40, 40)).save(image_path)

        from photo_reviewer.scanner import start_rebuild_index

        job = start_rebuild_index(self.folder, self.db, self.cfg)
        wait_for_job(job.id)

        rows = self.db.list_photos(folder=self.folder, exclude_deleted=False)
        self.assertEqual(len(rows), 1, [r["filename"] for r in rows])
        row = rows[0]
        self.assertEqual(row["filename"], "pair.JPG")
        self.assertEqual(row["raw_path"], raw_path)
        self.assertEqual(row["score"], 7.7)
        self.assertEqual(row["tags"], ["来自RAW"])
        self.assertEqual(row["title"], "只有RAW时的标题")

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

    def test_stale_settings_rows_are_pruned_on_start(self) -> None:
        """Rows written by older versions must not survive into a new run.

        ``host``/``port``/``debug`` are environment-only now, and a persisted
        ``debug=true`` used to quietly re-enable the Werkzeug debugger on a
        later start. ``thumb_size`` was renamed to ``gallery_thumb_size``.
        """
        self.db.save_settings(
            {
                "host": "0.0.0.0",
                "port": "9999",
                "debug": "true",
                "thumb_size": "320",
                "model": "keep-me",
            }
        )
        before = self.db.get_settings()
        self.assertIn("host", before)

        # Opening the app is what prunes them.
        create_app(self.cfg, self.db)
        after = self.db.get_settings()
        for key in ("host", "port", "debug", "thumb_size"):
            self.assertNotIn(key, after)
        self.assertEqual(after["model"], "keep-me")
        self.assertEqual(self.cfg.host, "127.0.0.1")

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
            {"technical": 7.5, "memory": 9.0, "uniqueness": 7.8},
            ["夏日"],
            "很好",
            "标题",
            "杭州",
        )
        listing = self.client.get("/api/photos").get_json()["photos"][0]
        self.assertEqual(listing["dimensions"]["technical"], 7.5)
        self.assertEqual(listing["dimensions"]["memory"], 9.0)
        self.assertNotIn("composition", listing["dimensions"])
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

    def test_permanent_delete_can_keep_the_raw(self) -> None:
        """The raw checkbox on the permanent-delete dialog is honoured."""
        source = os.path.join(self.folder, "keep.jpg")
        raw = os.path.join(self.folder, "keep.NEF")
        Image.new("RGB", (16, 16)).save(source)
        Path(raw).write_bytes(b"raw-bytes")
        self.db.upsert_photo(
            {
                "path": source,
                "folder": self.folder,
                "filename": "keep.jpg",
                "status": "analyzed",
                "raw_path": raw,
            }
        )
        photo_id = self.db.get_photo_by_path(source)["id"]
        self.client.post("/api/delete", json={"ids": [photo_id]})
        trashed_image = self.db.get_photo(photo_id)["path"]
        trashed_raw = os.path.join(os.path.dirname(trashed_image), "keep.NEF")

        resp = self.client.post(
            "/api/delete/permanent", json={"ids": [photo_id], "delete_raw": False}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["raw_removed"], 0)
        self.assertEqual(len(body["purged"]), 1)
        self.assertFalse(os.path.exists(trashed_image))
        self.assertTrue(os.path.exists(trashed_raw), "raw should have been kept")
        self.assertIsNone(self.db.get_photo(photo_id))

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
        body = resp.get_json()
        self.assertEqual(body["purged"][0]["id"], photo_id)
        self.assertEqual(body["raw_removed"], 1)
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
        self.assertEqual(photo["raw_size"], 3)

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

    def test_raw_sibling_size_is_reported_for_the_detail_page(self) -> None:
        """The detail page shows "10 MB + 30 MB" for a paired photo."""
        image = os.path.join(self.folder, "PAIR.JPG")
        raw = os.path.join(self.folder, "PAIR.NEF")
        Image.new("RGB", (640, 480), (120, 90, 60)).save(image, "JPEG")
        Path(raw).write_bytes(b"N" * 4096)
        self.db.upsert_photo(
            {
                "path": image,
                "folder": self.folder,
                "filename": "PAIR.JPG",
                "status": "analyzed",
                "score": 7.0,
                "raw_path": raw,
            }
        )
        app = create_app(self.cfg, self.db)
        client = app.test_client()
        photo_id = self.db.get_photo_by_path(image)["id"]

        listed = client.get("/api/photos").get_json()["photos"][0]
        self.assertEqual(listed["raw_size"], 4096)

        detail = client.get(f"/api/photo/{photo_id}").get_json()
        self.assertEqual(detail["raw_size"], 4096)
        self.assertTrue(detail["has_raw"])

    def test_a_missing_raw_sibling_reports_zero_not_an_error(self) -> None:
        image = os.path.join(self.folder, "GONE.JPG")
        Image.new("RGB", (64, 48), (120, 90, 60)).save(image, "JPEG")
        self.db.upsert_photo(
            {
                "path": image,
                "folder": self.folder,
                "filename": "GONE.JPG",
                "status": "analyzed",
                "raw_path": os.path.join(self.folder, "GONE.NEF"),
            }
        )
        app = create_app(self.cfg, self.db)
        client = app.test_client()
        photo = client.get("/api/photos").get_json()["photos"][0]
        self.assertTrue(photo["has_raw"])
        self.assertEqual(photo["raw_size"], 0)

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

    def test_jpg_is_imported_when_only_the_raw_exists_locally(self) -> None:
        """The mirror case: the local folder has the raw half only.

        Selecting 仅JPG or JPG+RAW must still bring the image over, otherwise
        the album keeps showing the raw-only card the user asked to complete.
        """
        self._seed_card()
        self._raw(self.local, "DCIM", "100D3500", "DSC_0001.NEF")
        for policy in (RAW_POLICY_IMAGE_FIRST, RAW_POLICY_BOTH):
            plan = plan_import(self.card, self.local, self.cfg, raw_policy=policy)
            names = sorted(os.path.basename(c.relative_path) for c in plan.copies)
            self.assertIn("DSC_0001.JPG", names, policy)
        # 仅RAW has no reason to pull the image in.
        plan = plan_import(
            self.card, self.local, self.cfg, raw_policy=RAW_POLICY_RAW_ONLY
        )
        names = sorted(os.path.basename(c.relative_path) for c in plan.copies)
        self.assertNotIn("DSC_0001.JPG", names)

    def test_both_policies_are_stable_when_only_one_half_differs(self) -> None:
        """An identical half is left alone in incremental mode."""
        self._seed_card()
        self._image(self.local, "DCIM", "100D3500", "DSC_0001.JPG", color=(1, 1, 1))
        self._raw(self.local, "DCIM", "100D3500", "DSC_0001.NEF", payload=b"raw-payload")
        # Only the raw matches the card byte for byte.
        stat = os.stat(os.path.join(self.card, "DCIM", "100D3500", "DSC_0001.NEF"))
        os.utime(
            os.path.join(self.local, "DCIM", "100D3500", "DSC_0001.NEF"),
            (stat.st_atime, stat.st_mtime),
        )
        plan = plan_import(
            self.card, self.local, self.cfg, raw_policy=RAW_POLICY_BOTH
        )
        names = sorted(os.path.basename(c.relative_path) for c in plan.copies)
        self.assertEqual(names, ["DSC_0002.JPG", "DSC_0003.NEF"])

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


class CardSyncTests(unittest.TestCase):
    """Camera-album pairings and the queued card work.

    The "card" is an ordinary temporary directory: as far as this module is
    concerned a card is any path recorded as the source of an album, and using a
    real directory keeps the tests offline and hermetically sealed.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.card = os.path.join(self.tmp.name, "card")
        self.album = os.path.join(self.tmp.name, "album")
        os.makedirs(os.path.join(self.card, "DCIM", "100D3500"))
        os.makedirs(os.path.join(self.album, "DCIM", "100D3500"))
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _pair(self, stem="DSC_0001", *, on_card=True, in_album=True) -> tuple[str, str]:
        """Create a same-named jpg+raw on both sides and return their paths."""
        album_dir = os.path.join(self.album, "DCIM", "100D3500")
        card_dir = os.path.join(self.card, "DCIM", "100D3500")
        os.makedirs(album_dir, exist_ok=True)
        os.makedirs(card_dir, exist_ok=True)
        image = os.path.join(album_dir, f"{stem}.JPG")
        raw = os.path.join(album_dir, f"{stem}.NEF")
        if in_album:
            Image.new("RGB", (20, 15), (120, 90, 60)).save(image, "JPEG")
            Path(raw).write_bytes(b"raw-payload" * 8)
        if on_card:
            Image.new("RGB", (20, 15), (60, 90, 120)).save(
                os.path.join(card_dir, f"{stem}.JPG"), "JPEG"
            )
            Path(os.path.join(card_dir, f"{stem}.NEF")).write_bytes(
                b"card-raw-payload" * 8
            )
        return image, raw

    def _register(self) -> dict:
        """Record the pairing, the way a finished import does."""
        return record_pairing(self.db, self.album, self.card)

    def _target(self):
        return resolve_card(self.album, self.card, volume_id(self.card))

    def _track(self, image, raw) -> int:
        """Index the pair as one photo row carrying its raw sibling."""
        self.db.upsert_photo(
            {
                "path": image,
                "folder": self.album,
                "filename": os.path.basename(image),
                "status": "analyzed",
                "raw_path": raw,
            }
        )
        return self.db.get_photo_by_path(image)["id"]

    def _card_files(self, *sub) -> list[str]:
        root = os.path.join(self.card, *sub)
        return sorted(os.listdir(root)) if os.path.isdir(root) else []

    def _card_trash_day(self) -> str:
        """The single day folder the card's trash created during a test."""
        days = self._card_files(self.cfg.trash_dir_name)
        self.assertEqual(len(days), 1, days)
        return days[0]

    def _queue_offline(self, name: str, is_raw: bool = False, photo_id=None) -> None:
        """Queue a deletion the way an unplugged card produces one."""
        offline = self._target()
        offline.connected = False
        delete_from_card(
            self.db,
            offline,
            os.path.join("DCIM", "100D3500", name),
            self.cfg.trash_dir_name,
            filename=name,
            is_raw=is_raw,
            size=2048,
            photo_id=photo_id,
        )

    # -- pairings and status ------------------------------------------------

    def test_a_folder_only_becomes_a_camera_album_after_a_pairing(self) -> None:
        self.assertEqual(card_status(self.db, self.album), [])
        recorded = self._register()
        self.assertEqual(recorded["album"], canonical_path(self.album))
        entry = card_status(self.db, self.album)[0]
        self.assertTrue(entry["connected"])
        self.assertTrue(entry["same_volume"])
        self.assertEqual(entry["state"], "connected")
        self.assertEqual(entry["pending"]["total"], 0)

    def test_a_missing_card_reports_disconnected_not_an_error(self) -> None:
        self._register()
        shutil.rmtree(self.card)
        entry = card_status(self.db, self.album)[0]
        self.assertFalse(entry["connected"])
        self.assertFalse(entry["usable"])
        self.assertEqual(entry["state"], "disconnected")

    def test_a_reused_drive_letter_is_detected(self) -> None:
        """A card that is not the recorded one must never be written to."""
        self._register()
        self.db.upsert_card_album(canonical_path(self.album), canonical_path(self.card), 1)
        target = resolve_card(canonical_path(self.album), canonical_path(self.card), 1)
        self.assertTrue(target.connected)
        self.assertFalse(target.same_volume)
        self.assertFalse(target.usable)
        self.assertEqual(target.state, "volume_mismatch")

    def test_volume_id_is_stable_for_one_directory(self) -> None:
        first = volume_id(self.card)
        self.assertIsNotNone(first)
        self.assertEqual(first, volume_id(os.path.join(self.card, "DCIM")))

    def test_relative_to_album_rejects_an_outside_path(self) -> None:
        self.assertEqual(
            relative_to_album(self.album, os.path.join(self.album, "DCIM", "x.jpg")),
            os.path.join("DCIM", "x.jpg"),
        )
        self.assertIsNone(
            relative_to_album(self.album, os.path.join(self.tmp.name, "y.jpg"))
        )
        self.assertIsNone(relative_to_album(self.album, None))

    def test_card_path_for_refuses_to_escape_the_card(self) -> None:
        self.assertEqual(
            card_path_for(self.card, os.path.join("DCIM", "a.jpg")),
            os.path.join(canonical_path(self.card), "DCIM", "a.jpg"),
        )
        for bad in ("..\\secret.txt", "../secret.txt", os.path.abspath("C:\\Windows")):
            with self.assertRaises(CardError):
                card_path_for(self.card, bad)

    def test_a_card_whose_root_moved_a_level_is_still_found(self) -> None:
        """Importing ``E:\\`` instead of ``E:\\DCIM`` shifts every path.

        The tail of the recorded path still matches exactly one file, so the
        photo is found rather than being reported as missing.
        """
        self._pair()
        self._register()
        # Pretend the import took the drive root: one level deeper on disk.
        moved = os.path.join(self.card, "card-root")
        shutil.move(os.path.join(self.card, "DCIM"), os.path.join(moved, "DCIM"))
        found = find_on_card(
            self._target(), os.path.join("DCIM", "100D3500", "DSC_0001.JPG")
        )
        self.assertIsNotNone(found)
        self.assertTrue(found.endswith(os.path.join("100D3500", "DSC_0001.JPG")))

    def test_an_ambiguous_file_name_is_never_guessed(self) -> None:
        """Numbered camera folders repeat names; a guess would delete a stranger.

        With no path that matches, two candidates must resolve to "not found"
        rather than to an arbitrary one of them.
        """
        self._pair()
        self._register()
        other = os.path.join(self.card, "DCIM", "101D3500")
        os.makedirs(other, exist_ok=True)
        Image.new("RGB", (20, 15), (9, 9, 9)).save(
            os.path.join(other, "DSC_0001.JPG"), "JPEG"
        )

        found = find_on_card(self._target(), os.path.join("DCIM", "999D3500", "DSC_0001.JPG"))
        self.assertIsNone(found)
        # The exact path still wins over the tie, so nothing is lost by this.
        exact = find_on_card(
            self._target(), os.path.join("DCIM", "100D3500", "DSC_0001.JPG")
        )
        self.assertIsNotNone(exact)

    # -- deleting -----------------------------------------------------------

    def test_deleting_on_a_connected_card_trashes_both_halves(self) -> None:
        image, raw = self._pair()
        self._register()
        target = self._target()

        first = delete_from_card(
            self.db,
            target,
            os.path.join("DCIM", "100D3500", "DSC_0001.JPG"),
            self.cfg.trash_dir_name,
            filename="DSC_0001.JPG",
        )
        second = delete_from_card(
            self.db,
            target,
            os.path.join("DCIM", "100D3500", "DSC_0001.NEF"),
            self.cfg.trash_dir_name,
            filename="DSC_0001.NEF",
            is_raw=True,
        )
        self.assertTrue(first["synced"])
        self.assertTrue(second["synced"])
        self.assertEqual(self._card_files("DCIM", "100D3500"), [])
        day = self._card_trash_day()
        self.assertEqual(
            self._card_files(self.cfg.trash_dir_name, day),
            ["DSC_0001.JPG", "DSC_0001.NEF"],
        )
        # The album copies are untouched: this module only moves card files.
        self.assertTrue(os.path.exists(image))
        self.assertTrue(os.path.exists(raw))
        self.assertEqual(len(self.db.card_actions(source=canonical_path(self.card))), 2)

    def test_deleting_without_a_card_queues_the_intent(self) -> None:
        self._pair(on_card=True)
        self._register()
        # The card is unplugged: its directory is simply gone.
        shutil.rmtree(self.card)
        outcome = delete_from_card(
            self.db,
            self._target(),
            os.path.join("DCIM", "100D3500", "DSC_0001.JPG"),
            self.cfg.trash_dir_name,
            filename="DSC_0001.JPG",
            size=1234,
        )
        self.assertFalse(outcome["synced"])
        self.assertTrue(outcome["queued"])
        queued = self.db.card_actions(state=STATE_PENDING)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["size"], 1234)

    def test_a_read_only_card_queues_instead_of_failing(self) -> None:
        self._pair()
        self._register()
        target = self._target()
        target.writable = False
        outcome = delete_from_card(
            self.db,
            target,
            os.path.join("DCIM", "100D3500", "DSC_0001.JPG"),
            self.cfg.trash_dir_name,
        )
        self.assertTrue(outcome["queued"])
        self.assertEqual(
            self._card_files("DCIM", "100D3500"), ["DSC_0001.JPG", "DSC_0001.NEF"]
        )

    def test_a_displaced_card_is_never_written_to(self) -> None:
        self._pair()
        self._register()
        target = self._target()
        target.same_volume = False
        outcome = delete_from_card(
            self.db,
            target,
            os.path.join("DCIM", "100D3500", "DSC_0001.JPG"),
            self.cfg.trash_dir_name,
        )
        self.assertTrue(outcome["queued"])
        self.assertEqual(
            self._card_files("DCIM", "100D3500"), ["DSC_0001.JPG", "DSC_0001.NEF"]
        )

    def test_a_file_missing_from_the_card_is_not_queued_forever(self) -> None:
        self._pair(on_card=False)
        self._register()
        outcome = delete_from_card(
            self.db,
            self._target(),
            os.path.join("DCIM", "100D3500", "DSC_0001.JPG"),
            self.cfg.trash_dir_name,
        )
        self.assertFalse(outcome["synced"])
        self.assertFalse(outcome["queued"])
        self.assertEqual(self.db.card_actions(), [])

    def test_a_twice_deleted_photo_keeps_its_finished_action(self) -> None:
        """Re-deleting must not turn an applied action back into a queued one."""
        self._pair()
        self._register()
        target = self._target()
        relative = os.path.join("DCIM", "100D3500", "DSC_0001.JPG")
        first = delete_from_card(self.db, target, relative, self.cfg.trash_dir_name)
        self.assertTrue(first["synced"])
        again = delete_from_card(self.db, target, relative, self.cfg.trash_dir_name)
        self.assertFalse(again["queued"])
        self.assertTrue(again["already"])
        self.assertEqual(self.db.card_actions(state=STATE_PENDING), [])
        applied = self.db.card_actions(state=STATE_APPLIED)
        self.assertEqual(len(applied), 1, "the finished action must survive")

    # -- applying the queue ------------------------------------------------

    def test_applying_the_queue_moves_the_card_files(self) -> None:
        self._pair(on_card=True)
        self._register()
        self._queue_offline("DSC_0001.JPG")
        self._queue_offline("DSC_0001.NEF", is_raw=True)
        self.assertEqual(len(self.db.card_actions(state=STATE_PENDING)), 2)

        result = apply_pending(self.db, self.card, self.cfg.trash_dir_name)
        self.assertEqual(result["applied"], 2)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(self._card_files("DCIM", "100D3500"), [])
        # Still on the card, but parked in its trash.
        day = self._card_trash_day()
        self.assertEqual(
            self._card_files(self.cfg.trash_dir_name, day),
            ["DSC_0001.JPG", "DSC_0001.NEF"],
        )
        # Running it again is a no-op rather than an error.
        self.assertEqual(
            apply_pending(self.db, self.card, self.cfg.trash_dir_name)["applied"], 0
        )

    def test_include_raw_false_leaves_the_raw_on_the_card(self) -> None:
        self._pair()
        self._register()
        self._queue_offline("DSC_0001.JPG")
        self._queue_offline("DSC_0001.NEF", is_raw=True)
        result = apply_pending(
            self.db, self.card, self.cfg.trash_dir_name, include_raw=False
        )
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self._card_files("DCIM", "100D3500"), ["DSC_0001.NEF"])
        self.assertEqual(len(self.db.card_actions(state=STATE_PENDING)), 1)

    def test_a_queued_delete_of_a_missing_file_is_dropped(self) -> None:
        self._pair(on_card=False)
        self._register()
        self._queue_offline("DSC_0001.JPG")
        result = apply_pending(self.db, self.card, self.cfg.trash_dir_name)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["remaining"], 0)

    def test_the_queue_cannot_be_applied_on_the_wrong_card(self) -> None:
        self._pair()
        self._register()
        self._queue_offline("DSC_0001.JPG")
        self.db.upsert_card_album(
            canonical_path(self.album), canonical_path(self.card), 1
        )
        result = apply_pending(self.db, self.card, self.cfg.trash_dir_name)
        self.assertEqual(result["applied"], 0)
        self.assertTrue(result["failed"])
        self.assertEqual(
            self._card_files("DCIM", "100D3500"), ["DSC_0001.JPG", "DSC_0001.NEF"]
        )

    # -- preview, restore, purge ------------------------------------------

    def test_the_preview_describes_what_will_be_deleted(self) -> None:
        self._pair()
        self._register()
        self._queue_offline("DSC_0001.JPG")
        self._queue_offline("DSC_0001.NEF", is_raw=True)
        preview = pending_preview(self.db, self.card)
        summary = preview["summary"]
        self.assertEqual(summary["delete_count"], 2)
        self.assertEqual(summary["raw_count"], 1)
        self.assertEqual(summary["total_bytes"], 4096)
        self.assertEqual(summary["missing_count"], 0)
        self.assertTrue(preview["ready"])
        self.assertEqual(
            sorted(item["filename"] for item in preview["items"]),
            ["DSC_0001.JPG", "DSC_0001.NEF"],
        )
        self.assertTrue(all(item["on_card"] for item in preview["items"]))
        self.assertEqual([i["kind"] for i in preview["items"]], [KIND_DELETE] * 2)

    def test_the_preview_flags_files_the_card_no_longer_has(self) -> None:
        self._pair(on_card=False)
        self._register()
        self._queue_offline("DSC_0001.JPG")
        preview = pending_preview(self.db, self.card)
        self.assertEqual(preview["summary"]["missing_count"], 1)
        self.assertFalse(preview["items"][0]["on_card"])

    def test_restoring_cancels_a_queued_delete(self) -> None:
        self._pair()
        self._register()
        self._queue_offline("DSC_0001.JPG")
        outcome = restore_to_card(
            self.db,
            self._target(),
            os.path.join("DCIM", "100D3500", "DSC_0001.JPG"),
        )
        self.assertTrue(outcome["cancelled"])
        self.assertEqual(self.db.card_actions(), [])
        self.assertEqual(len(self._card_files("DCIM", "100D3500")), 2)

    def test_restoring_puts_an_applied_delete_back_on_the_card(self) -> None:
        self._pair()
        self._register()
        target = self._target()
        relative = os.path.join("DCIM", "100D3500", "DSC_0001.JPG")
        delete_from_card(self.db, target, relative, self.cfg.trash_dir_name)
        self.assertEqual(self._card_files("DCIM", "100D3500"), ["DSC_0001.NEF"])

        outcome = restore_to_card(self.db, target, relative)
        self.assertTrue(outcome["synced"])
        self.assertEqual(
            self._card_files("DCIM", "100D3500"), ["DSC_0001.JPG", "DSC_0001.NEF"]
        )
        self.assertEqual(self.db.card_actions(), [])

    def test_restoring_without_a_card_queues_a_restore(self) -> None:
        self._pair()
        self._register()
        target = self._target()
        relative = os.path.join("DCIM", "100D3500", "DSC_0001.JPG")
        delete_from_card(self.db, target, relative, self.cfg.trash_dir_name)

        offline = self._target()
        offline.connected = False
        outcome = restore_to_card(self.db, offline, relative)
        self.assertTrue(outcome["queued"])
        self.assertEqual(
            [a["kind"] for a in self.db.card_actions(state=STATE_PENDING)], ["restore"]
        )

        result = apply_pending(self.db, self.card, self.cfg.trash_dir_name)
        self.assertEqual(result["restored"], 1)
        self.assertEqual(
            self._card_files("DCIM", "100D3500"), ["DSC_0001.JPG", "DSC_0001.NEF"]
        )

    def test_deleting_again_after_a_restore_is_a_real_deletion(self) -> None:
        self._pair()
        self._register()
        target = self._target()
        relative = os.path.join("DCIM", "100D3500", "DSC_0001.JPG")
        delete_from_card(self.db, target, relative, self.cfg.trash_dir_name)
        restore_to_card(self.db, target, relative)

        outcome = delete_from_card(self.db, target, relative, self.cfg.trash_dir_name)
        # The card has the file again, so this is a fresh, real deletion.
        self.assertTrue(outcome["synced"])
        self.assertEqual(self._card_files("DCIM", "100D3500"), ["DSC_0001.NEF"])

    def test_permanent_delete_erases_the_card_copy(self) -> None:
        image, raw = self._pair()
        self._register()
        photo_id = self._track(image, raw)
        target = self._target()
        for name, is_raw in (("DSC_0001.JPG", False), ("DSC_0001.NEF", True)):
            delete_from_card(
                self.db,
                target,
                os.path.join("DCIM", "100D3500", name),
                self.cfg.trash_dir_name,
                filename=name,
                is_raw=is_raw,
                photo_id=photo_id,
            )
        result = purge_for_photo(self.db, target, photo_id)
        self.assertEqual(result["purged"], 2)
        self.assertEqual(result["deferred"], 0)
        self.assertEqual(self.db.card_actions(), [])

    def test_permanent_delete_on_an_absent_card_is_deferred(self) -> None:
        """The card copy is erased next time, not silently forgotten."""
        image, raw = self._pair(on_card=False)
        self._register()
        photo_id = self._track(image, raw)
        self._queue_offline("DSC_0001.JPG", photo_id=photo_id)

        offline = self._target()
        offline.connected = False
        result = purge_for_photo(self.db, offline, photo_id)
        self.assertEqual(result["purged"], 0)
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(len(self.db.card_actions(state=STATE_ERASE)), 1)

        # The card comes back with the file still on it: the erase runs.
        self._pair(on_card=True)
        applied = apply_pending(self.db, self.card, self.cfg.trash_dir_name)
        self.assertEqual(applied["erased"], 1)
        self.assertEqual(self._card_files("DCIM", "100D3500"), ["DSC_0001.NEF"])

    def test_forgetting_a_card_drops_the_queue_without_touching_files(self) -> None:
        from photo_reviewer.cards import forget_card

        self._pair()
        self._register()
        self._queue_offline("DSC_0001.JPG")
        result = forget_card(self.db, self.album)
        self.assertEqual(result["pairings_removed"], 1)
        self.assertEqual(self.db.card_actions(), [])
        self.assertEqual(card_status(self.db, self.album), [])
        self.assertEqual(len(self._card_files("DCIM", "100D3500")), 2)


class CardApiTests(unittest.TestCase):
    """The HTTP surface of the camera-album feature."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.card = os.path.join(self.tmp.name, "card")
        self.album = os.path.join(self.tmp.name, "album")
        os.makedirs(os.path.join(self.card, "DCIM", "100D3500"))
        os.makedirs(os.path.join(self.album, "DCIM", "100D3500"))
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _photo(self, stem="DSC_0001", on_card=True) -> tuple[int, str, str]:
        album_dir = os.path.join(self.album, "DCIM", "100D3500")
        card_dir = os.path.join(self.card, "DCIM", "100D3500")
        image = os.path.join(album_dir, f"{stem}.JPG")
        raw = os.path.join(album_dir, f"{stem}.NEF")
        Image.new("RGB", (20, 15), (120, 90, 60)).save(image, "JPEG")
        Path(raw).write_bytes(b"raw-payload" * 8)
        if on_card:
            Image.new("RGB", (20, 15), (60, 90, 120)).save(
                os.path.join(card_dir, f"{stem}.JPG"), "JPEG"
            )
            Path(os.path.join(card_dir, f"{stem}.NEF")).write_bytes(b"card-raw" * 8)
        self.db.upsert_photo(
            {
                "path": image,
                "folder": self.album,
                "filename": f"{stem}.JPG",
                "status": "analyzed",
                "raw_path": raw,
            }
        )
        record_pairing(self.db, self.album, self.card)
        return self.db.get_photo_by_path(image)["id"], image, raw

    def _card_trash_day(self) -> tuple[str, str]:
        """Return ``(trash root, day folder)`` for the card's own trash."""
        root = os.path.join(self.card, self.cfg.trash_dir_name)
        days = os.listdir(root)
        self.assertEqual(len(days), 1, days)
        return root, os.path.join(root, days[0])

    def test_status_reports_the_current_album(self) -> None:
        self._photo()
        resp = self.client.get(f"/api/card/status?folder={self.album}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(len(body["cards"]), 1)
        self.assertTrue(body["current"]["connected"])
        self.assertEqual(body["current"]["pending"]["total"], 0)

    def test_delete_with_sync_card_moves_both_halves_off_the_card(self) -> None:
        photo_id, image, raw = self._photo()
        resp = self.client.post(
            "/api/delete", json={"ids": [photo_id], "sync_card": True}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["card"]["synced"], 2)
        self.assertEqual(body["card"]["queued"], 0)
        self.assertFalse(os.path.exists(image))
        self.assertFalse(os.path.exists(raw))
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.card, "DCIM", "100D3500"))), []
        )
        # The card copies were parked in the card's own trash, not deleted.
        _root, day = self._card_trash_day()
        self.assertEqual(
            sorted(os.listdir(day)), ["DSC_0001.JPG", "DSC_0001.NEF"]
        )

    def test_delete_without_sync_leaves_the_card_alone(self) -> None:
        photo_id, _image, _raw = self._photo()
        resp = self.client.post(
            "/api/delete", json={"ids": [photo_id], "sync_card": False}
        )
        body = resp.get_json()
        self.assertEqual(body["card"]["synced"], 0)
        self.assertEqual(body["card"]["queued"], 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.card, "DCIM", "100D3500"))),
            ["DSC_0001.JPG", "DSC_0001.NEF"],
        )
        self.assertEqual(self.db.card_actions(), [])

    def test_delete_queues_when_the_card_is_away(self) -> None:
        photo_id, _image, _raw = self._photo(on_card=False)
        shutil.rmtree(self.card)
        resp = self.client.post(
            "/api/delete", json={"ids": [photo_id], "sync_card": False}
        )
        self.assertEqual(resp.get_json()["card"]["queued"], 2)
        preview = self.client.post("/api/card/pending", json={}).get_json()["preview"]
        self.assertEqual(preview["summary"]["delete_count"], 2)
        self.assertEqual(preview["summary"]["raw_count"], 1)
        self.assertFalse(preview["ready"])

    def test_restore_with_sync_card_puts_both_halves_back(self) -> None:
        photo_id, _image, _raw = self._photo()
        self.client.post("/api/delete", json={"ids": [photo_id], "sync_card": True})
        restore_resp = self.client.post(
            "/api/restore", json={"ids": [photo_id], "sync_card": True}
        )
        self.assertEqual(restore_resp.get_json()["card"]["restored"], 2)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.card, "DCIM", "100D3500"))),
            ["DSC_0001.JPG", "DSC_0001.NEF"],
        )

    def test_card_sync_endpoint_needs_a_source(self) -> None:
        self.assertEqual(self.client.post("/api/card/sync", json={}).status_code, 400)

    def test_card_sync_endpoint_applies_a_queued_delete(self) -> None:
        photo_id, _image, _raw = self._photo(on_card=False)
        shutil.rmtree(self.card)
        self.client.post("/api/delete", json={"ids": [photo_id], "sync_card": False})
        # The card comes back with its files.
        os.makedirs(os.path.join(self.card, "DCIM", "100D3500"))
        Image.new("RGB", (20, 15), (60, 90, 120)).save(
            os.path.join(self.card, "DCIM", "100D3500", "DSC_0001.JPG"), "JPEG"
        )
        resp = self.client.post("/api/card/sync", json={"source": self.card})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["applied"], 1)
        self.assertEqual(
            os.listdir(os.path.join(self.card, "DCIM", "100D3500")), []
        )

    def test_folder_remove_forgets_the_card_link(self) -> None:
        self._photo()
        resp = self.client.post("/api/folder/remove", json={"folder": self.album})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["card_links_removed"], 1)
        self.assertEqual(card_status(self.db, self.album), [])

    def test_permanent_delete_clears_the_card_trash(self) -> None:
        photo_id, _image, _raw = self._photo()
        self.client.post("/api/delete", json={"ids": [photo_id], "sync_card": True})
        _root, day = self._card_trash_day()
        self.assertEqual(len(os.listdir(day)), 2)

        resp = self.client.post("/api/delete/permanent", json={"ids": [photo_id]})
        self.assertEqual(resp.get_json()["card_removed"], 2)
        self.assertEqual(os.listdir(day), [])

    def test_an_import_records_the_camera_album(self) -> None:
        """The import is what turns a folder into a camera album."""
        source = os.path.join(self.tmp.name, "incoming")
        os.makedirs(os.path.join(source, "DCIM"))
        Image.new("RGB", (20, 15), (10, 20, 30)).save(
            os.path.join(source, "DCIM", "DSC_0009.JPG"), "JPEG"
        )
        resp = self.client.post(
            "/api/import/run",
            json={
                "source": source,
                "target": self.album,
                "mode": "incremental",
                "after": "copy",
            },
        )
        self.assertEqual(resp.status_code, 200)
        wait_for_job(resp.get_json()["job_id"])
        entry = card_status(self.db, self.album)
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["source"], canonical_path(source))
        self.assertTrue(entry[0]["connected"])


class UiStateTests(unittest.TestCase):
    """The folder history and the open album live in the database.

    They used to live in ``localStorage``, so clearing the browser's site data
    threw away the album's memory. These tests pin the behaviour that replaced
    it: the state survives a fresh client, and the endpoint cannot be used to
    write arbitrary rows.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_state_starts_empty(self) -> None:
        body = self.client.get("/api/ui-state").get_json()
        self.assertEqual(body["state"], {})

    def test_folder_and_history_round_trip(self) -> None:
        resp = self.client.post(
            "/api/ui-state",
            json={"folder": self.folder, "folder_history": [self.folder, "D:\\其他"]},
        )
        self.assertEqual(resp.status_code, 200)
        # A brand-new client (a cleared browser) sees the same state.
        fresh = self.app.test_client()
        state = fresh.get("/api/ui-state").get_json()["state"]
        self.assertEqual(state["folder"], self.folder)
        self.assertEqual(state["folder_history"], [self.folder, "D:\\其他"])

    def test_a_partial_update_keeps_the_other_keys(self) -> None:
        self.client.post("/api/ui-state", json={"folder": self.folder})
        self.client.post("/api/ui-state", json={"import_source": "E:\\DCIM"})
        state = self.client.get("/api/ui-state").get_json()["state"]
        self.assertEqual(state["folder"], self.folder)
        self.assertEqual(state["import_source"], "E:\\DCIM")

    def test_the_history_is_deduplicated_and_capped(self) -> None:
        history = [f"D:\\相册\\{i}" for i in range(40)]
        history.insert(3, "d:\\相册\\0")  # same path, different case
        self.client.post("/api/ui-state", json={"folder_history": history})
        stored = self.client.get("/api/ui-state").get_json()["state"]["folder_history"]
        self.assertEqual(len(stored), 20)
        self.assertEqual(len({s.lower() for s in stored}), 20)

    def test_an_unknown_key_is_rejected(self) -> None:
        resp = self.client.post("/api/ui-state", json={"evil": "payload"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.client.get("/api/ui-state").get_json()["state"], {})
        stored = self.db.get_settings()
        self.assertNotIn("evil", stored)
        self.assertNotIn("payload", stored.values())

    def test_a_malformed_history_is_rejected(self) -> None:
        resp = self.client.post("/api/ui-state", json={"folder_history": "D:\\相册"})
        self.assertEqual(resp.status_code, 400)

    def test_the_directory_browser_hides_the_apps_own_folders(self) -> None:
        """Cache and trash directories are bookkeeping, not photo folders."""
        for name in (self.cfg.cache_dir_name, self.cfg.trash_dir_name, "旅行"):
            os.makedirs(os.path.join(self.folder, name))
        body = self.client.get(f"/api/dirs?path={self.folder}").get_json()
        names = [d["name"] for d in body["directories"]]
        self.assertEqual(names, ["旅行"])


class RemoveFileRetryTests(unittest.TestCase):
    """Windows refuses to unlink a file another process has open.

    Antivirus, the search indexer and Explorer's preview handler all hold files
    briefly. Without a retry, a bin sweep over thousands of photos randomly
    reports a handful as "删除文件失败" even though nothing is wrong.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "busy.jpg")
        Path(self.path).write_bytes(b"payload")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_free_file_is_removed_immediately(self) -> None:
        _remove_file(self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_a_missing_file_is_not_an_error(self) -> None:
        os.remove(self.path)
        _remove_file(self.path)  # must not raise

    def test_a_transient_lock_is_ridden_out(self) -> None:
        real_remove = os.remove
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(32, "The process cannot access the file")
            return real_remove(path)

        with (
            patch("photo_reviewer.server.os.remove", side_effect=flaky),
            patch("photo_reviewer.server.time.sleep") as sleep,
        ):
            _remove_file(self.path)
        self.assertEqual(calls["n"], 3)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(sleep.called, "the retry must back off between attempts")

    def test_a_permanent_lock_still_raises(self) -> None:
        with (
            patch(
                "photo_reviewer.server.os.remove",
                side_effect=PermissionError(32, "locked"),
            ),
            patch("photo_reviewer.server.time.sleep"),
            self.assertRaises(PermissionError),
        ):
            _remove_file(self.path)
        self.assertTrue(os.path.exists(self.path))

    def test_a_different_error_is_not_retried(self) -> None:
        with (
            patch(
                "photo_reviewer.server.os.remove",
                side_effect=IsADirectoryError(21, "is a directory"),
            ) as remove,
            self.assertRaises(IsADirectoryError),
        ):
            _remove_file(self.path)
        self.assertEqual(remove.call_count, 1)


class EmptyTrashJobTests(unittest.TestCase):
    """Clearing the bin is a tracked job, so the toolbar can show progress.

    The sweep used to run inside the request; a full bin can hold thousands of
    files, and the UI had nothing to display while it worked.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        self.trash = os.path.join(self.folder, ".photo-trash", "20260101")
        os.makedirs(self.trash)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        # The sweep runs in a daemon thread. Wait it out before the database and
        # the temporary folder go away, so a failing assertion cannot leave a
        # thread writing into a half-deleted directory.
        deadline = time.time() + 10
        while time.time() < deadline:
            if not any(job.status in ("pending", "running") for job in JOBS.list()):
                break
            time.sleep(0.02)
        self.db.close()
        self.tmp.cleanup()

    def _trashed(self, name: str) -> int:
        """Create one photo already sitting in the trash."""
        path = os.path.join(self.trash, name)
        Path(path).write_bytes(b"trashed" * 16)
        self.db.upsert_photo(
            {
                "path": path,
                "original_path": os.path.join(self.folder, name),
                "folder": self.folder,
                "filename": name,
                "status": "deleted",
            }
        )
        return self.db.get_photo_by_path(path)["id"]

    def test_the_background_job_reports_progress_and_finishes(self) -> None:
        ids = [self._trashed(f"{i}.jpg") for i in range(5)]
        resp = self.client.post(
            "/api/trash/empty", json={"background": True, "folder": self.folder}
        )
        self.assertEqual(resp.status_code, 200)
        job_id = resp.get_json()["job_id"]
        job = wait_for_job(job_id)
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.phase, "trash")
        self.assertEqual(job.total, 5)
        self.assertEqual(job.processed, 5)
        self.assertEqual(job.report["deleted"], 5)
        for photo_id in ids:
            self.assertIsNone(self.db.get_photo(photo_id))
        self.assertEqual(os.listdir(self.trash), [])

    def test_the_job_reports_an_empty_bin_without_failing(self) -> None:
        resp = self.client.post("/api/trash/empty", json={"background": True})
        job = wait_for_job(resp.get_json()["job_id"])
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.total, 0)
        self.assertEqual(job.report["deleted"], 0)

    def test_progress_is_visible_while_the_job_runs(self) -> None:
        for i in range(4):
            self._trashed(f"{i}.jpg")
        job_id = self.client.post(
            "/api/trash/empty", json={"background": True, "folder": self.folder}
        ).get_json()["job_id"]
        # The status endpoint is what the toolbar polls.
        body = self.client.get(f"/api/scan/status/{job_id}").get_json()
        self.assertEqual(body["phase"], "trash")
        self.assertEqual(body["total"], 4)
        self.assertIn("processed", body)
        wait_for_job(job_id)

    def test_cancelling_the_job_stops_the_sweep(self) -> None:
        for i in range(3):
            self._trashed(f"{i}.jpg")
        job_id = self.client.post(
            "/api/trash/empty", json={"background": True, "folder": self.folder}
        ).get_json()["job_id"]
        wait_for_job(job_id)
        # Cancelling after the fact is refused, which is what the button relies
        # on to report honestly.
        cancel = self.client.post(f"/api/scan/cancel/{job_id}").get_json()
        self.assertFalse(cancel["cancelled"])

    def test_the_synchronous_form_still_returns_counts(self) -> None:
        """Scripts and the other tests rely on the direct answer."""
        self._trashed("direct.jpg")
        body = self.client.post("/api/trash/empty", json={"folder": self.folder}).get_json()
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(body["total"], 1)
        self.assertFalse(body["cancelled"])

    def test_the_refusal_guard_still_applies_in_the_job(self) -> None:
        """A stale row pointing at a live photo is refused inside the job too."""
        live = os.path.join(self.folder, "live.jpg")
        Image.new("RGB", (24, 18), (120, 90, 60)).save(live, "JPEG")
        self.db.upsert_photo(
            {
                "path": live,
                "original_path": live,
                "folder": self.folder,
                "filename": "live.jpg",
                "status": "deleted",
            }
        )
        job_id = self.client.post(
            "/api/trash/empty", json={"background": True, "folder": self.folder}
        ).get_json()["job_id"]
        job = wait_for_job(job_id)
        self.assertEqual(job.report["deleted"], 0)
        self.assertTrue(job.report["errors"])
        self.assertTrue(os.path.exists(live), "the original was deleted!")


class DeletionSafetyTests(unittest.TestCase):
    """Nothing outside a trash directory is ever erased.

    Both routes that remove files for good - "彻底移除" and "清空回收站" - must
    only ever act on photos the user already moved to the trash. A stale row
    whose path still points at a live photo is the case that matters: an
    interrupted move, or a hand-edited database, must not turn a confirmation
    dialog into the loss of an original.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _photo(self, name="keep.jpg", *, status="deleted", folder=None):
        path = os.path.join(self.folder, name)
        Image.new("RGB", (24, 18), (120, 90, 60)).save(path, "JPEG")
        self.db.upsert_photo(
            {
                "path": path,
                "original_path": path,
                "folder": folder or self.folder,
                "filename": name,
                "status": status,
            }
        )
        return self.db.get_photo_by_path(path)["id"], path

    def test_permanent_delete_refuses_a_path_outside_the_trash(self) -> None:
        photo_id, live_path = self._photo()
        resp = self.client.post("/api/delete/permanent", json={"ids": [photo_id]})
        body = resp.get_json()
        self.assertEqual(body["purged"], [])
        self.assertTrue(body["errors"])
        self.assertTrue(os.path.exists(live_path), "the original was deleted!")
        # The row is kept as well, so the album does not lose track of the file.
        self.assertIsNotNone(self.db.get_photo(photo_id))

    def test_permanent_delete_still_removes_a_trashed_file(self) -> None:
        photo_id, _live = self._photo(status="analyzed")
        self.client.post("/api/delete", json={"ids": [photo_id]})
        trashed = self.db.get_photo(photo_id)["path"]
        self.assertIn(self.cfg.trash_dir_name, trashed)
        resp = self.client.post("/api/delete/permanent", json={"ids": [photo_id]})
        self.assertEqual(len(resp.get_json()["purged"]), 1)
        self.assertFalse(os.path.exists(trashed))
        self.assertIsNone(self.db.get_photo(photo_id))

    def test_emptying_the_bin_refuses_a_live_photo(self) -> None:
        photo_id, live_path = self._photo("stale.jpg")
        resp = self.client.post("/api/trash/empty", json={})
        body = resp.get_json()
        self.assertEqual(body["deleted"], 0)
        self.assertTrue(body["errors"], "the refusal must be reported, not silent")
        self.assertTrue(os.path.exists(live_path), "the original was deleted!")
        self.assertIsNotNone(self.db.get_photo(photo_id))

    def test_the_trash_check_rejects_a_lookalike_directory(self) -> None:
        """``.photo-trash-backup`` is not the trash, however similar it looks."""
        lookalike = os.path.join(self.folder, self.cfg.trash_dir_name + "-backup")
        os.makedirs(lookalike)
        photo_id, live_path = self._photo("x.jpg", folder=self.folder)
        self.db.mark_deleted([photo_id], {photo_id: os.path.join(lookalike, "x.jpg")})
        if not os.path.exists(live_path):
            Image.new("RGB", (10, 10)).save(live_path, "JPEG")
        shutil.move(live_path, os.path.join(lookalike, "x.jpg"))
        moved = os.path.join(lookalike, "x.jpg")
        resp = self.client.post("/api/delete/permanent", json={"ids": [photo_id]})
        self.assertEqual(resp.get_json()["purged"], [])
        self.assertTrue(os.path.exists(moved))

    def test_a_cache_cleanup_cannot_escape_its_directory(self) -> None:
        """A cache directory name of "." would point at the photos themselves."""
        with self.assertRaises(ValueError):
            cache_dir_for_folder(self.folder, ".")
        with self.assertRaises(ValueError):
            cache_dir_for_folder(self.folder, "")
        with self.assertRaises(ValueError):
            cache_dir_for_folder(self.folder, "../elsewhere")
        # The normal names still resolve as before.
        self.assertEqual(
            cache_dir_for_folder(self.folder, ".photo-review-cache"),
            Path(self.folder) / ".photo-review-cache",
        )

    def test_a_photo_is_never_indexed_from_a_trash_or_cache_directory(self) -> None:
        """Scanning cannot pick a trashed photo up and re-admit it as a photo."""
        trash = os.path.join(self.folder, self.cfg.trash_dir_name, "20260101")
        os.makedirs(trash)
        Image.new("RGB", (16, 16)).save(os.path.join(trash, "old.jpg"), "JPEG")
        Image.new("RGB", (16, 16)).save(os.path.join(self.folder, "new.jpg"), "JPEG")
        found = [
            os.path.basename(p)
            for p in discover_images(self.folder, skip_dirs=[self.cfg.trash_dir_name])
        ]
        self.assertEqual(found, ["new.jpg"])

    def test_is_in_named_dir_only_looks_at_ancestors(self) -> None:
        inside = os.path.join(self.folder, self.cfg.trash_dir_name, "20260101", "a.jpg")
        self.assertTrue(is_in_named_dir(inside, self.cfg.trash_dir_name))
        self.assertFalse(is_in_named_dir(os.path.join(self.folder, "a.jpg"), self.cfg.trash_dir_name))
        # A file *named* like the trash directory is not inside it.
        self.assertFalse(
            is_in_named_dir(
                os.path.join(self.folder, self.cfg.trash_dir_name), self.cfg.trash_dir_name
            )
        )
        self.assertFalse(is_in_named_dir(None, self.cfg.trash_dir_name))
        self.assertFalse(is_in_named_dir(inside, ""))


class OrientationTests(unittest.TestCase):
    """Portrait photos must be recorded - and rendered - the way viewers show them.

    Cameras store the sensor's landscape frame and mark the rotation in EXIF.
    An earlier version recorded the raw header size, so every portrait shot was
    filed as landscape and the gallery drew it sideways.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.cache = os.path.join(self.folder, "cache")
        self.db = Database(os.path.join(self.tmp.name, "library.db"))
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _oriented(self, name: str, orientation: int, size=(600, 400)) -> str:
        path = os.path.join(self.folder, name)
        exif = Image.Exif()
        exif[274] = orientation
        Image.new("RGB", size, (150, 120, 90)).save(path, "JPEG", exif=exif)
        return path

    def test_a_rotated_tag_swaps_the_measured_size(self) -> None:
        for orientation in (6, 8):
            path = self._oriented(f"rot{orientation}.jpg", orientation)
            self.assertEqual(image_size_on_disk(path), (400, 600), orientation)

    def test_an_upright_photo_keeps_its_size(self) -> None:
        for orientation in (1, 3):
            path = self._oriented(f"flat{orientation}.jpg", orientation)
            self.assertEqual(image_size_on_disk(path), (600, 400), orientation)

    def test_a_missing_file_reports_nothing(self) -> None:
        self.assertIsNone(image_size_on_disk(os.path.join(self.folder, "nope.jpg")))

    def test_make_proxy_returns_display_dimensions_and_writes_upright_files(self) -> None:
        path = self._oriented("portrait.jpg", 6)
        proxy, width, height, _hash, thumb = make_proxy(
            path, cache_dir=self.cache, max_edge=400, thumb_size=200
        )
        self.assertEqual((width, height), (400, 600))
        for produced in (proxy, thumb):
            with Image.open(produced) as img:
                self.assertGreater(img.size[1], img.size[0], produced)
                # The pixels are baked in, so the tag must not be re-applied.
                self.assertIn((img.getexif() or {}).get(274), (None, 1), produced)

    def test_make_proxy_gives_rotated_photos_a_distinct_cache_key(self) -> None:
        """An upright photo must not pick up the sideways proxy of the old build."""
        upright = self._oriented("plain.jpg", 1)
        rotated = self._oriented("turned.jpg", 6)
        base = os.path.basename(
            make_proxy(upright, cache_dir=self.cache)[0]
        ).replace("_proxy.jpg", "")
        turned = os.path.basename(
            make_proxy(rotated, cache_dir=self.cache)[0]
        ).replace("_proxy.jpg", "")
        self.assertNotEqual(base, turned)

    def test_the_repair_fixes_sizes_and_drops_the_stale_cache(self) -> None:
        portrait = self._oriented("shot.jpg", 6)
        # Cache a sideways proxy under the key the photo had before the fix.
        stale = os.path.join(self.cache, "legacy_proxy.jpg")
        os.makedirs(self.cache, exist_ok=True)
        Image.new("RGB", (600, 400)).save(stale, "JPEG")
        self.db.upsert_photo(
            {
                "path": portrait,
                "folder": self.folder,
                "filename": "shot.jpg",
                "status": "analyzed",
                "width": 600,
                "height": 400,
                "thumb_path": stale,
                "proxy_path": stale,
            }
        )
        pid = self.db.get_photo_by_path(portrait)["id"]

        report = repair_orientation_sizes(self.db, self.cfg)
        self.assertEqual(report["fixed"], 1)
        self.assertEqual(report["cache_dropped"], 1)

        row = self.db.get_photo(pid)
        self.assertEqual((row["width"], row["height"]), (400, 600))
        self.assertIsNone(row["proxy_path"])
        self.assertFalse(os.path.exists(stale))

    def test_the_repair_leaves_upright_photos_alone(self) -> None:
        path = self._oriented("fine.jpg", 1)
        keep = os.path.join(self.cache, "keep_proxy.jpg")
        os.makedirs(self.cache, exist_ok=True)
        Image.new("RGB", (600, 400)).save(keep, "JPEG")
        self.db.upsert_photo(
            {
                "path": path,
                "folder": self.folder,
                "filename": "fine.jpg",
                "status": "analyzed",
                "width": 600,
                "height": 400,
                "thumb_path": keep,
                "proxy_path": keep,
            }
        )
        report = repair_orientation_sizes(self.db, self.cfg)
        self.assertEqual(report["fixed"], 0)
        self.assertTrue(os.path.exists(keep))

    def test_the_repair_runs_once_per_database(self) -> None:
        self.assertTrue(needs_orientation_repair(self.db))
        run_startup_repairs(self.db, self.cfg)
        self.assertFalse(needs_orientation_repair(self.db))
        self.assertFalse(needs_dimensions_repair(self.db))
        # A second call is a no-op rather than a second pass.
        self.assertEqual(run_startup_repairs(self.db, self.cfg), {})

    def test_rows_without_a_file_are_skipped(self) -> None:
        self.db.upsert_photo(
            {
                "path": os.path.join(self.folder, "gone.jpg"),
                "folder": self.folder,
                "filename": "gone.jpg",
                "status": "pending",
                "width": 600,
                "height": 400,
            }
        )
        report = repair_orientation_sizes(self.db, self.cfg)
        self.assertEqual(report["checked"], 0)


class DimensionMergeTests(unittest.TestCase):
    """「构图」 was merged into 「技术」; old rows have to follow.

    The two were both craft judgements about the frame, and asking the model for
    both produced numbers that tracked each other. The retired key must not
    survive anywhere - not in the database, not in an analysis result, not in a
    hand-edited settings row.
    """

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

    def _photo(self, name: str, dimensions) -> int:
        path = os.path.join(self.folder, name)
        Image.new("RGB", (24, 18), (120, 90, 60)).save(path, "JPEG")
        self.db.upsert_photo(
            {
                "path": path,
                "folder": self.folder,
                "filename": name,
                "status": "analyzed",
                "score": 8.0,
                "dimensions": dimensions,
            }
        )
        return self.db.get_photo_by_path(path)["id"]

    def test_the_merge_averages_the_two_scores(self) -> None:
        pid = self._photo("a.jpg", {"technical": 6, "composition": 8, "memory": 9})
        report = repair_dimension_keys(self.db)
        self.assertEqual(report["merged"], 1)
        dims = self.db.get_photo(pid)["dimensions"]
        self.assertEqual(dims["technical"], 7.0)
        self.assertNotIn("composition", dims)
        self.assertEqual(dims["memory"], 9)

    def test_rows_without_the_retired_key_are_untouched(self) -> None:
        pid = self._photo("b.jpg", {"technical": 7, "memory": 8, "uniqueness": 6})
        report = repair_dimension_keys(self.db)
        self.assertEqual(report["merged"], 0)
        self.assertEqual(self.db.get_photo(pid)["dimensions"]["technical"], 7)

    def test_an_empty_dimensions_object_stays_empty(self) -> None:
        """The UI reads {} as "not scored yet"; it must not gain a fake score."""
        pid = self._photo("c.jpg", {})
        repair_dimension_keys(self.db)
        self.assertEqual(self.db.get_photo(pid)["dimensions"], {})

    def test_the_merge_is_idempotent(self) -> None:
        pid = self._photo("d.jpg", {"technical": 5, "composition": 5})
        repair_dimension_keys(self.db)
        repair_dimension_keys(self.db)
        self.assertEqual(self.db.get_photo(pid)["dimensions"]["technical"], 5)

    def test_the_model_result_folds_a_four_key_answer(self) -> None:
        """An old prompt (or a cached one) still gets its composition counted."""
        result = _normalize_dimensions(
            {"technical": 6, "composition": 8, "memory": 9, "uniqueness": 4}
        )
        self.assertEqual(
            result, {"technical": 7.0, "memory": 9.0, "uniqueness": 4.0}
        )

    def test_the_model_result_keeps_three_keys_unchanged(self) -> None:
        result = _normalize_dimensions({"technical": 6, "memory": 9, "uniqueness": 4})
        self.assertEqual(
            result, {"technical": 6.0, "memory": 9.0, "uniqueness": 4.0}
        )

    def test_the_prompt_no_longer_asks_for_four_dimensions(self) -> None:
        self.assertNotIn("composition", JSON_FORMAT_SUFFIX)
        self.assertNotIn("composition", DEFAULT_STYLE_PROMPT)
        self.assertIn("technical", DEFAULT_STYLE_PROMPT)
        for preset in PROMPT_PRESETS.values():
            self.assertNotIn("composition", preset)

    def test_the_schema_has_no_composition_column(self) -> None:
        """The score lives inside the JSON blob, so the column list is the test."""
        with self.db as _:
            pass
        raw = sqlite3.connect(self.cfg.db_path)
        columns = [row[1] for row in raw.execute("PRAGMA table_info(photos)")]
        raw.close()
        self.assertNotIn("composition", columns)

    def test_the_api_never_returns_the_retired_key(self) -> None:
        """A row that has not been migrated yet must not leak the old key."""
        pid = self._photo("e.jpg", {"technical": 5, "memory": 6})
        # Write straight past upsert_photo so the legacy shape is really stored.
        raw = sqlite3.connect(self.cfg.db_path)
        raw.execute(
            "UPDATE photos SET dimensions=? WHERE id=?",
            (json.dumps({"technical": 5, "composition": 7, "memory": 6}), pid),
        )
        raw.commit()
        raw.close()

        app = create_app(self.cfg, self.db)
        client = app.test_client()
        listing = client.get("/api/photos").get_json()["photos"][0]
        self.assertNotIn("composition", listing["dimensions"])

        # And an edit that only touches another field still drops it.
        resp = client.post(
            f"/api/photo/{pid}/edit",
            json={"score": 8.0, "dimensions": {"memory": 6}},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("composition", resp.get_json()["dimensions"])


class CardStatusPayloadTests(unittest.TestCase):
    """The toolbar badge is driven by /api/card/status.

    The frontend matches the state strings against its own table, so the payload
    shape is part of the contract and is worth pinning.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.card = os.path.join(self.tmp.name, "card")
        self.album = os.path.join(self.tmp.name, "album")
        os.makedirs(os.path.join(self.card, "DCIM"))
        os.makedirs(os.path.join(self.album, "DCIM"))
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_a_paired_album_reports_the_connected_state(self) -> None:
        record_pairing(self.db, self.album, self.card)
        body = self.client.get(
            "/api/card/status?folder=" + quote(self.album)
        ).get_json()
        current = body["current"]
        self.assertIsNotNone(current)
        self.assertEqual(current["state"], "connected")
        self.assertTrue(current["usable"])
        self.assertEqual(current["pending"]["total"], 0)

    def test_an_album_with_no_card_reports_nothing_current(self) -> None:
        body = self.client.get(
            "/api/card/status?folder=" + quote(self.album)
        ).get_json()
        self.assertIsNone(body["current"])
        self.assertEqual(body["cards"], [])

    def test_a_removed_card_reports_the_disconnected_state(self) -> None:
        record_pairing(self.db, self.album, self.card)
        shutil.rmtree(self.card)
        body = self.client.get(
            "/api/card/status?folder=" + quote(self.album)
        ).get_json()
        self.assertEqual(body["current"]["state"], "disconnected")
        self.assertFalse(body["current"]["usable"])

    def test_the_queue_shows_up_in_the_payload(self) -> None:
        record_pairing(self.db, self.album, self.card)
        self.db.save_card_action(
            {
                "album": self.album,
                "source": self.card,
                "volume": None,
                "kind": KIND_DELETE,
                "state": STATE_PENDING,
                "relative_path": "DCIM/DSC_0001.JPG",
                "card_path": os.path.join(self.card, "DCIM", "DSC_0001.JPG"),
                "filename": "DSC_0001.JPG",
                "size": 128,
                "is_raw": 0,
                "photo_id": 1,
            }
        )
        body = self.client.get(
            "/api/card/status?folder=" + quote(self.album)
        ).get_json()
        self.assertEqual(body["current"]["pending"]["total"], 1)


class GeocodingDisabledTests(unittest.TestCase):
    """Choosing "不使用" keeps coordinates and never touches the network."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.cfg = Config(
            data_dir=self.tmp.name,
            db_path=os.path.join(self.tmp.name, "library.db"),
            geocoding_provider="none",
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _gps_photo(self, location: str | None = None) -> tuple[int, str]:
        path = os.path.join(self.folder, "gps.jpg")
        exif = Image.Exif()
        exif[274] = 1
        Image.new("RGB", (40, 30), (120, 90, 60)).save(path, "JPEG", exif=exif)
        self.db.upsert_photo(
            {
                "path": path,
                "folder": self.folder,
                "filename": "gps.jpg",
                "status": "analyzed",
                "score": 6.0,
                "location": location,
                "exif": {"latitude": 30.25, "longitude": 120.17},
            }
        )
        return self.db.get_photo_by_path(path)["id"], path

    def test_the_config_endpoint_stores_none(self) -> None:
        resp = self.client.post("/api/config", json={"geocoding_provider": "none"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["geocoding_provider"], "none")
        # And it survives a reload from the database.
        fresh = Config(data_dir=self.tmp.name, db_path=self.cfg.db_path)
        app = create_app(fresh, self.db)
        self.assertEqual(
            app.test_client().get("/api/config").get_json()["geocoding_provider"],
            "none",
        )

    def test_an_unknown_provider_falls_back_instead_of_being_stored(self) -> None:
        resp = self.client.post("/api/config", json={"geocoding_provider": "bogus"})
        self.assertEqual(resp.get_json()["geocoding_provider"], "nominatim")

    def test_an_empty_provider_still_means_nominatim(self) -> None:
        """The pre-existing behaviour for a blank value must not change."""
        resp = self.client.post("/api/config", json={"geocoding_provider": ""})
        self.assertEqual(resp.get_json()["geocoding_provider"], "nominatim")

    def test_no_request_is_made_when_disabled(self) -> None:
        photo_id, _path = self._gps_photo()
        with patch("photo_reviewer.server.reverse_geocode") as geocode:
            self.client.get(f"/api/photo/{photo_id}")
        geocode.assert_not_called()

    def test_the_detail_payload_keeps_the_coordinates(self) -> None:
        photo_id, _path = self._gps_photo()
        body = self.client.get(f"/api/photo/{photo_id}").get_json()
        self.assertEqual(body["exif"]["latitude"], 30.25)
        self.assertEqual(body["exif"]["longitude"], 120.17)

    def test_enrich_metadata_keeps_coordinates_when_disabled(self) -> None:
        """A re-analysis must not quietly re-resolve an old address."""
        from photo_reviewer.pipeline import enrich_metadata, prepare_proxy

        path = os.path.join(self.folder, "again.jpg")
        Image.new("RGB", (40, 30), (60, 90, 120)).save(path, "JPEG")
        prepared = prepare_proxy(path, self.folder, self.cfg)
        with patch("photo_reviewer.pipeline.reverse_geocode") as geocode:
            enrich_metadata(prepared, self.cfg, existing_location="杭州市西湖区")
        geocode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
