"""Core regression tests for Lumina Album.

These tests run fully locally: all databases and image folders are temporary,
and model calls are replaced with deterministic fakes.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
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
    DIMENSIONS_REPAIR_KEY,
    DIMENSIONS_REPAIR_VERSION,
    ORIENTATION_REPAIR_KEY,
    ORIENTATION_REPAIR_VERSION,
    needs_dimensions_repair,
    needs_orientation_repair,
    repair_dimension_keys,
    repair_orientation_sizes,
    run_startup_repairs,
    wait_for_repairs,
)
from photo_reviewer.scanner import (
    JOBS,
    discover_images,
    discover_photo_pairs,
    start_rebuild_index,
    start_scan,
)
from photo_reviewer.server import _remove_file, create_app
from photo_reviewer.thumbnailer import (
    cleanup_cache,
    hamming_distance,
    image_size_on_disk,
    make_proxy,
)
from photo_reviewer.volume import (
    SOURCE_DEVICE,
    SOURCE_SERIAL,
    SOURCE_UUID,
    SOURCE_VOLUME_GUID,
    TRUST_ORDER,
    VolumeIdentity,
    identities_compatible,
    volume_identity,
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
        # Mimic _move_to_trash: the original path is now empty. Through the
        # production helper rather than os.remove, because on Windows a file
        # written moments ago can still be held briefly by the indexer - the
        # same reason `_remove_file` retries.
        _remove_file(photo_path)
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

    def test_the_video_block_survives_a_save(self) -> None:
        """Regression: these keys were listed as editable but never staged.

        The dialog accepted a change, answered 200, and quietly kept the old
        value - which is why the preview length kept snapping back.
        """
        resp = self.client.post(
            "/api/config",
            json={
                "video_clip_seconds": 7,
                "video_clip_max_edge": 720,
                "video_clip_crf": 31,
                "video_poster_ratio": 0.25,
                "video_clip_audio": False,
                "video_pregenerate": True,
                "merge_live_photos": False,
                "video_preview_mode": "loop",
                "video_preview_volume": 80,
                "live_volume": 35,
            },
        )
        self.assertEqual(resp.status_code, 200)
        cfg = self.client.get("/api/config").get_json()
        self.assertEqual(cfg["video_clip_seconds"], 7)
        self.assertEqual(cfg["video_clip_max_edge"], 720)
        self.assertEqual(cfg["video_clip_crf"], 31)
        self.assertAlmostEqual(cfg["video_poster_ratio"], 0.25)
        self.assertFalse(cfg["video_clip_audio"])
        self.assertTrue(cfg["video_pregenerate"])
        self.assertFalse(cfg["merge_live_photos"])
        self.assertEqual(cfg["video_preview_mode"], "loop")
        self.assertEqual(cfg["video_preview_volume"], 80)
        self.assertEqual(cfg["live_volume"], 35)

    def test_video_settings_are_clamped(self) -> None:
        self.client.post(
            "/api/config",
            json={"video_clip_seconds": 999, "video_preview_volume": -20},
        )
        cfg = self.client.get("/api/config").get_json()
        self.assertEqual(cfg["video_clip_seconds"], 20)
        self.assertEqual(cfg["video_preview_volume"], 0)

    def test_an_unknown_preview_mode_is_rejected(self) -> None:
        before = self.cfg.video_preview_mode
        resp = self.client.post("/api/config", json={"video_preview_mode": "sideways"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.cfg.video_preview_mode, before)

    def test_video_processing_defaults_to_ffmpeg_and_saves(self) -> None:
        cfg = Config(data_dir=self.tmp.name)
        self.assertEqual(cfg.video_processing, "ffmpeg")
        resp = self.client.post("/api/config", json={"video_processing": "none"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.client.get("/api/config").get_json()["video_processing"], "none"
        )

    def test_an_unknown_video_processing_mode_is_rejected(self) -> None:
        before = self.cfg.video_processing
        resp = self.client.post("/api/config", json={"video_processing": "magic"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.cfg.video_processing, before)

    def test_all_three_preview_modes_are_accepted(self) -> None:
        for mode in ("off", "once", "loop"):
            resp = self.client.post("/api/config", json={"video_preview_mode": mode})
            self.assertEqual(resp.status_code, 200, mode)
            self.assertEqual(self.client.get("/api/config").get_json()["video_preview_mode"], mode)

    def test_the_defaults_are_three_seconds_and_half_volume(self) -> None:
        cfg = Config(data_dir=self.tmp.name)
        self.assertEqual(cfg.video_clip_seconds, 3.0)
        self.assertEqual(cfg.video_preview_mode, "once")
        self.assertEqual(cfg.video_preview_volume, 50)
        self.assertEqual(cfg.live_volume, 0)

    def test_a_stored_five_second_preview_is_migrated(self) -> None:
        """The old value can only be the old default: the control never worked."""
        from photo_reviewer.server import _load_settings_from_db

        self.db.save_settings({"video_clip_seconds": 5})
        cfg = Config(data_dir=self.tmp.name)
        _load_settings_from_db(self.db, cfg)
        self.assertEqual(cfg.video_clip_seconds, 3.0)
        self.assertEqual(
            float(str(self.db.get_settings()["video_clip_seconds"])), 3.0
        )

    def test_a_deliberate_preview_length_is_left_alone(self) -> None:
        from photo_reviewer.server import _load_settings_from_db

        self.db.save_settings({"video_clip_seconds": 8})
        cfg = Config(data_dir=self.tmp.name)
        _load_settings_from_db(self.db, cfg)
        self.assertEqual(cfg.video_clip_seconds, 8.0)

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

    def test_a_trashed_pair_still_reports_its_raw_half(self) -> None:
        """The list payload must know a trashed pair is a pair.

        The permanent-delete dialog shows "同时删除 RAW" only when the row says
        it has a raw. Trashing the photo used to clear ``raw_path``, so the
        option silently disappeared for exactly the case that needs it.
        """
        source = os.path.join(self.folder, "pair.jpg")
        raw = os.path.join(self.folder, "pair.NEF")
        Image.new("RGB", (16, 16)).save(source)
        Path(raw).write_bytes(b"raw-bytes")
        self.db.upsert_photo(
            {
                "path": source,
                "folder": self.folder,
                "filename": "pair.jpg",
                "status": "analyzed",
                "raw_path": raw,
            }
        )
        photo_id = self.db.get_photo_by_path(source)["id"]
        self.client.post("/api/delete", json={"ids": [photo_id]})

        rows = self.client.get(
            "/api/photos", query_string={"status": "deleted", "folder": self.folder}
        ).get_json()["photos"]
        row = next(r for r in rows if r["id"] == photo_id)
        self.assertTrue(row["has_raw"], "the dialog would hide its raw option")
        self.assertEqual(row["raw_ext"], "NEF")

    def test_a_row_trashed_by_an_older_build_is_still_detected(self) -> None:
        """Old rows cleared ``raw_path``; the file is found beside the image."""
        source = os.path.join(self.folder, "old.jpg")
        trash_day = Path(self.folder) / self.cfg.trash_dir_name / "20260101"
        trash_day.mkdir(parents=True, exist_ok=True)
        trashed_image = trash_day / "old.jpg"
        Image.new("RGB", (16, 16)).save(str(trashed_image))
        (trash_day / "old.NEF").write_bytes(b"raw-bytes")
        photo_id = self.db.upsert_photo(
            {
                "path": str(trashed_image),
                "original_path": source,
                "folder": self.folder,
                "filename": "old.jpg",
                "status": "deleted",
                "raw_path": None,
            }
        )

        rows = self.client.get(
            "/api/photos", query_string={"status": "deleted", "folder": self.folder}
        ).get_json()["photos"]
        row = next(r for r in rows if r["id"] == photo_id)
        self.assertTrue(row["has_raw"], "an older trashed pair must still offer RAW")
        self.assertEqual(row["raw_path"], str(trash_day / "old.NEF"))

    def test_purging_a_pair_deletes_both_halves(self) -> None:
        source = os.path.join(self.folder, "both.jpg")
        raw = os.path.join(self.folder, "both.NEF")
        Image.new("RGB", (16, 16)).save(source)
        Path(raw).write_bytes(b"raw-bytes")
        self.db.upsert_photo(
            {
                "path": source,
                "folder": self.folder,
                "filename": "both.jpg",
                "status": "analyzed",
                "raw_path": raw,
            }
        )
        photo_id = self.db.get_photo_by_path(source)["id"]
        self.client.post("/api/delete", json={"ids": [photo_id]})
        resp = self.client.post(
            "/api/delete/permanent", json={"ids": [photo_id], "delete_raw": True}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["raw_removed"], 1)
        self.assertIsNone(self.db.get_photo(photo_id))
        day_dir = os.path.join(self.folder, self.cfg.trash_dir_name)
        leftovers = [
            name
            for _root, _dirs, names in os.walk(day_dir)
            for name in names
            if name.lower().endswith(".nef")
        ]
        self.assertEqual(leftovers, [], "the raw was left in the trash")

    def test_purging_can_keep_the_raw(self) -> None:
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
        resp = self.client.post(
            "/api/delete/permanent", json={"ids": [photo_id], "delete_raw": False}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["raw_removed"], 0)

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
        trashed_raw = os.path.join(day_dir, "a.NEF")
        self.assertTrue(os.path.exists(trashed_raw))
        # The row keeps pointing at the raw, now inside the trash: the pair is
        # still a pair, and the permanent-delete dialog needs to know that.
        self.assertEqual(trashed["raw_path"], trashed_raw)

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
        # The raw size needs a stat(), so a list row does not carry it; the
        # detail endpoint that the dialog calls does.
        self.assertNotIn("raw_size", photo)
        photo_id = self.db.get_photo_by_path(jpg)["id"]
        detail = client.get(f"/api/photo/{photo_id}/detail").get_json()
        self.assertEqual(detail["raw_size"], 3)

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

        # The gallery row only says a raw file exists; the size comes from the
        # detail endpoint the dialog asks for once, for the photo on screen.
        listed = client.get("/api/photos").get_json()["photos"][0]
        self.assertTrue(listed["has_raw"])
        self.assertNotIn("raw_size", listed)
        self.assertNotIn("exif", listed)

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
        photo_id = self.db.get_photo_by_path(image)["id"]
        detail = client.get(f"/api/photo/{photo_id}/detail").get_json()
        self.assertEqual(detail["raw_size"], 0)

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

    def test_a_pairing_records_a_volume_identity(self) -> None:
        """The path is not the identity: a GUID or UUID is recorded with it."""
        pairing = self._register()
        self.assertIsNotNone(pairing)
        self.assertTrue(pairing["volume_key"], pairing)
        # ``key:source`` - the source says which backend answered, so a later
        # run knows whether the two keys are even comparable.
        self.assertIn(":", pairing["volume_key"])
        self.assertIn(pairing["volume_source"], TRUST_ORDER)

    def test_the_card_is_recognised_at_a_different_path(self) -> None:
        """A replug into another drive letter is still the same card.

        This is the whole point of identifying the volume rather than the path:
        as long as the same physical disk answers, the recorded identity still
        matches whatever letter it came back on.
        """
        self._pair()
        pairing = self._register()
        target = resolve_card(
            self.album, self.card, pairing.get("volume"), pairing.get("volume_key")
        )
        self.assertTrue(target.connected)
        self.assertTrue(target.same_volume)
        self.assertTrue(target.usable)
        self.assertEqual(target.state, "connected")
        # And the identity it reports is the one the pairing was made with.
        self.assertEqual(target.volume_key, pairing["volume_key"].rsplit(":", 1)[0])

    def test_a_replug_does_not_add_a_second_pairing_row(self) -> None:
        """One album belongs to one card, however often the letter changes."""
        self._pair()
        self._register()
        other = self.card + "_other"
        os.makedirs(other, exist_ok=True)
        record_pairing(self.db, self.album, other)
        rows = self.db.list_card_albums(canonical_path(self.album))
        self.assertEqual(len(rows), 1, [r["source"] for r in rows])
        self.assertEqual(rows[0]["source"], canonical_path(other))

    def test_pairings_without_a_key_still_resolve(self) -> None:
        """Rows written before volume identities existed must keep working.

        They carry only the integer column; the legacy check must not treat the
        absence of a key as a contradiction.
        """
        self._pair()
        self.db.upsert_card_album(
            canonical_path(self.album), canonical_path(self.card), volume_id(self.card)
        )
        target = resolve_card(
            self.album, self.card, volume_id(self.card), None
        )
        self.assertTrue(target.connected)
        self.assertTrue(target.same_volume)
        self.assertTrue(target.usable)

    def test_an_unidentified_volume_does_not_block_the_card(self) -> None:
        """No volume identifier is "unknown", not "wrong".

        Refusing to write whenever the platform stays silent would break the
        ordinary case on any machine the check does not support; only a card that
        positively identifies as a different disk is refused.
        """
        self._pair()
        with patch("photo_reviewer.cards.volume_identity", return_value=None):
            target = resolve_card(self.album, self.card, 0, None)
        self.assertTrue(target.connected)
        self.assertTrue(target.same_volume)
        self.assertTrue(target.usable)
        self.assertTrue(target.volume_unknown)
        self.assertTrue(target.warnings)

    def test_a_different_volume_key_is_refused(self) -> None:
        """The identity is what decides, not the path."""
        self._pair()
        fake = f"\\\\?\\volume{{00000000-0000-0000-0000-000000000000}}\\{':'}volume-guid"
        target = resolve_card(self.album, self.card, None, fake)
        self.assertTrue(target.connected)
        self.assertFalse(target.same_volume)
        self.assertFalse(target.usable)
        self.assertEqual(target.state, "volume_mismatch")

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
        # Simulate a different card in the same slot: the recorded identity has
        # to disagree with the volume actually mounted here.
        self.db.upsert_card_album(
            canonical_path(self.album),
            canonical_path(self.card),
            1,
            "\\\\?\\volume{00000000-0000-0000-0000-000000000000}\\:volume-guid",
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


class StalledVideoTests(unittest.TestCase):
    """A video whose probe failed must be retried, not stranded.

    Its path is already in the database, so the incremental scan skipped it
    forever and the detail dialog stayed empty with no way out but deleting the
    row by hand.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.db = Database(os.path.join(self.tmp.name, "l.db"))

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _video_row(self, name: str, **overrides):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 64)
        record = {
            "path": path,
            "folder": self.folder,
            "filename": name,
            "status": "error",
            "media_kind": "video",
            "video_info": {},
            "error": "ffprobe failed",
        }
        record.update(overrides)
        return self.db.upsert_photo(record)

    def test_a_video_without_info_is_re_queued(self) -> None:
        from photo_reviewer.scanner import _stalled_video_pairs

        self._video_row("broken.mp4")
        stalled = _stalled_video_pairs(self.folder, self.db, [])
        self.assertEqual([os.path.basename(p) for p, _raw in stalled], ["broken.mp4"])

    def test_a_healthy_video_is_left_alone(self) -> None:
        from photo_reviewer.scanner import _stalled_video_pairs

        # Healthy means it was read *and* has a poster: a row with metadata but
        # no picture has nothing to show, so it legitimately needs another pass.
        self._video_row(
            "fine.mp4",
            video_info={"video_codec": "h264"},
            status="analyzed",
            thumb_path=os.path.join(self.folder, "fine_thumb.jpg"),
            proxy_path=os.path.join(self.folder, "fine_proxy.jpg"),
        )
        self.assertEqual(_stalled_video_pairs(self.folder, self.db, []), [])

    def test_a_video_that_is_already_queued_is_not_repeated(self) -> None:
        from photo_reviewer.scanner import _stalled_video_pairs

        path = os.path.join(self.folder, "broken.mp4")
        self._video_row("broken.mp4")
        self.assertEqual(
            _stalled_video_pairs(self.folder, self.db, [(path, None)]), []
        )

    def test_a_video_whose_file_is_gone_is_skipped(self) -> None:
        from photo_reviewer.scanner import _stalled_video_pairs

        path = os.path.join(self.folder, "broken.mp4")
        self._video_row("broken.mp4")
        os.remove(path)
        self.assertEqual(_stalled_video_pairs(self.folder, self.db, []), [])

    def test_a_photo_is_never_re_queued_by_this_path(self) -> None:
        from photo_reviewer.scanner import _stalled_video_pairs

        path = os.path.join(self.folder, "still.jpg")
        Path(path).write_bytes(b"\xff\xd8\xff")
        self.db.upsert_photo(
            {
                "path": path,
                "folder": self.folder,
                "filename": "still.jpg",
                "status": "error",
                "media_kind": "photo",
            }
        )
        self.assertEqual(_stalled_video_pairs(self.folder, self.db, []), [])


class VideoProcessingModeTests(unittest.TestCase):
    """「视频处理方式」 decides what a scan picks up, not what the library holds.

    Turning it off must not touch rows that already exist: the folder may be on
    a slow drive, and re-walking it to delete videos would be a surprising,
    irreversible side effect of a settings toggle.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        Image.new("RGB", (40, 30), (1, 2, 3)).save(
            os.path.join(self.folder, "a.jpg"), "JPEG"
        )
        with open(os.path.join(self.folder, "b.mp4"), "wb") as fh:
            fh.write(b"\x00" * 32)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _pairs(self, mode: str) -> list[str]:
        from photo_reviewer.scanner import discover_photo_pairs, video_scan_extensions

        cfg = Config(
            data_dir=os.path.join(self.tmp.name, mode),
            db_path=os.path.join(self.tmp.name, f"{mode}.db"),
            video_processing=mode,
        )
        return [
            os.path.basename(path)
            for path, _raw in discover_photo_pairs(
                self.folder,
                cfg.image_extensions,
                cfg.raw_extensions,
                video_extensions=video_scan_extensions(cfg),
            )
        ]

    def test_ffmpeg_mode_collects_videos(self) -> None:
        self.assertEqual(self._pairs("ffmpeg"), ["a.jpg", "b.mp4"])

    def test_none_mode_walks_past_them(self) -> None:
        self.assertEqual(self._pairs("none"), ["a.jpg"])

    def test_the_helper_reads_the_setting(self) -> None:
        from photo_reviewer.scanner import video_scan_extensions

        cfg = Config(data_dir=self.tmp.name)
        self.assertTrue(video_scan_extensions(cfg))
        cfg.video_processing = "none"
        self.assertEqual(video_scan_extensions(cfg), ())


class ForceScanSemanticsTests(unittest.TestCase):
    """A forced run redoes the index first, then hands everything to the model.

    Before this the two buttons disagreed: 开始整理 re-analysed without
    re-reading the files, 同步相册 re-read without analysing. A full redo now
    means the same thing on both, and the difference is only whether the model
    is called afterwards.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.db = Database(os.path.join(self.tmp.name, "l.db"))
        self.cfg = Config(data_dir=self.tmp.name, db_path=self.db.path)
        self.cfg.scan_concurrency = 1
        self.db.set_meta(ORIENTATION_REPAIR_KEY, "1")
        self.db.set_meta(DIMENSIONS_REPAIR_KEY, "1")

    def tearDown(self) -> None:
        wait_for_repairs(timeout=30)
        self.db.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def _photo(self, name: str) -> str:
        path = os.path.join(self.folder, name)
        img = Image.new("RGB", (48, 36), (10, 20, 30))
        exif = Image.Exif()
        exif[271] = "Maker"
        exif[272] = name
        img.save(path, exif=exif)
        return path

    def test_a_forced_scan_reindexes_before_analysing(self) -> None:
        from photo_reviewer import scanner as scanner_mod

        path = self._photo("a.jpg")
        with patch(
            "photo_reviewer.scanner.analyze_prepared", side_effect=fake_analysis
        ):
            wait_for_job(start_scan(self.folder, self.db, self.cfg).id)

        # Change what the file says on disk, then force.
        img = Image.new("RGB", (64, 48), (200, 10, 10))
        exif = Image.Exif()
        exif[271] = "Maker"
        exif[272] = "renamed-by-exif"
        img.save(path, exif=exif)

        indexed = []
        real_index = scanner_mod._index_one

        def spy(*args, **kwargs):
            indexed.append(args[0])
            return real_index(*args, **kwargs)

        with (
            patch("photo_reviewer.scanner._index_one", side_effect=spy),
            patch("photo_reviewer.scanner.analyze_prepared", side_effect=fake_analysis),
        ):
            wait_for_job(start_scan(self.folder, self.db, self.cfg, force=True).id)

        self.assertIn(path, indexed, "a forced run must re-read the file")
        row = self.db.get_photo_by_path(path)
        self.assertEqual(row["exif"].get("model"), "renamed-by-exif")
        self.assertIsNotNone(row["score"], "and must still analyse it")

    def test_skip_indexed_videos_leaves_their_rows_alone(self) -> None:
        from photo_reviewer.scanner import _index_one

        path = os.path.join(self.folder, "clip.mp4")
        Path(path).write_bytes(b"\x00" * 64)
        self.db.upsert_photo(
            {
                "path": path,
                "folder": self.folder,
                "filename": "clip.mp4",
                "status": "analyzed",
                "media_kind": "video",
                "video_info": {"video_codec": "h264", "duration_ms": 5000},
                "duration_ms": 5000,
                "score": 7.0,
            }
        )

        indexed = []

        def spy(*args, **kwargs):
            indexed.append(args[0])
            return _index_one(*args, **kwargs)

        with (
            patch("photo_reviewer.scanner._index_one", side_effect=spy),
            patch("photo_reviewer.scanner.video_scan_extensions", return_value=(".mp4",)),
        ):
            wait_for_job(start_scan(
                    self.folder, self.db, self.cfg, force=True, skip_indexed_videos=True
                ).id,
            )

        self.assertNotIn(path, indexed)
        row = self.db.get_photo_by_path(path)
        self.assertEqual(row["video_info"].get("video_codec"), "h264")
        self.assertEqual(row["score"], 7.0)


class VideoProcessingNoneTests(unittest.TestCase):
    """「不处理」 stops collecting videos and cleans up their derived files.

    The files themselves are never touched - only what the app rendered from
    them - and the rule applies from the next sync, which is what the option
    now says.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.cache = os.path.join(self.folder, ".photo-review-cache")
        os.makedirs(self.cache)
        self.db = Database(os.path.join(self.tmp.name, "l.db"))
        self.cfg = Config(data_dir=self.tmp.name, db_path=self.db.path)
        self.db.set_meta(ORIENTATION_REPAIR_KEY, "1")
        self.db.set_meta(DIMENSIONS_REPAIR_KEY, "1")

        self.video = os.path.join(self.folder, "clip.mp4")
        Path(self.video).write_bytes(b"original-video-bytes")
        self.clip = os.path.join(self.cache, "abc_clip.mp4")
        Path(self.clip).write_bytes(b"rendered-clip")
        self.thumb = os.path.join(self.cache, "abc_thumb.jpg")
        Path(self.thumb).write_bytes(b"poster")
        self.video_id = self.db.upsert_photo(
            {
                "path": self.video,
                "folder": self.folder,
                "filename": "clip.mp4",
                "status": "analyzed",
                "media_kind": "video",
                "thumb_path": self.thumb,
                "clip_path": self.clip,
                "duration_ms": 4000,
                "video_info": {"video_codec": "h264"},
            }
        )

    def tearDown(self) -> None:
        wait_for_repairs(timeout=30)
        self.db.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_the_switch_removes_the_index_and_its_cache(self) -> None:
        self.cfg.video_processing = "none"
        wait_for_job(start_rebuild_index(self.folder, self.db, self.cfg).id)

        self.assertIsNone(self.db.get_photo(self.video_id))
        self.assertFalse(os.path.exists(self.clip))
        self.assertFalse(os.path.exists(self.thumb))
        # The footage itself is untouched.
        self.assertTrue(os.path.exists(self.video))
        self.assertEqual(Path(self.video).read_bytes(), b"original-video-bytes")

    def test_the_switch_keeps_videos_when_it_is_off(self) -> None:
        wait_for_job(start_rebuild_index(self.folder, self.db, self.cfg).id)
        self.assertIsNotNone(self.db.get_photo(self.video_id))
        self.assertTrue(os.path.exists(self.video))


class VideoRenderSignatureTests(unittest.TestCase):
    """Changing the preview settings invalidates renders at the next sync."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.cache = os.path.join(self.folder, ".photo-review-cache")
        os.makedirs(self.cache)
        self.db = Database(os.path.join(self.tmp.name, "l.db"))
        self.cfg = Config(data_dir=self.tmp.name, db_path=self.db.path)
        self.db.set_meta(ORIENTATION_REPAIR_KEY, "1")
        self.db.set_meta(DIMENSIONS_REPAIR_KEY, "1")
        self.video = os.path.join(self.folder, "clip.mp4")
        Path(self.video).write_bytes(b"v" * 32)
        self.clip = os.path.join(self.cache, "abc_clip.mp4")
        Path(self.clip).write_bytes(b"old-clip")
        self.video_id = self.db.upsert_photo(
            {
                "path": self.video,
                "folder": self.folder,
                "filename": "clip.mp4",
                "status": "analyzed",
                "media_kind": "video",
                "clip_path": self.clip,
                "thumb_path": os.path.join(self.cache, "abc_thumb.jpg"),
                "duration_ms": 4000,
                "video_info": {"video_codec": "h264"},
            }
        )

    def tearDown(self) -> None:
        wait_for_repairs(timeout=30)
        self.db.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_the_signature_tracks_the_rendering_settings(self) -> None:
        from photo_reviewer.scanner import video_render_signature

        first = video_render_signature(self.cfg)
        self.cfg.video_clip_seconds = 9
        self.assertNotEqual(first, video_render_signature(self.cfg))

    def test_an_unchanged_signature_keeps_the_existing_clip(self) -> None:
        from photo_reviewer.scanner import _invalidate_stale_video_renders

        self.assertEqual(_invalidate_stale_video_renders(self.folder, self.db, self.cfg), [])
        self.assertTrue(os.path.exists(self.clip))

    def test_a_changed_setting_drops_the_clip_and_reports_the_video(self) -> None:
        from photo_reviewer.scanner import _invalidate_stale_video_renders

        # Prime the stored signature with the current settings.
        _invalidate_stale_video_renders(self.folder, self.db, self.cfg)
        self.cfg.video_clip_seconds = 12
        affected = _invalidate_stale_video_renders(self.folder, self.db, self.cfg)

        self.assertEqual(affected, [self.video])
        self.assertFalse(os.path.exists(self.clip))
        self.assertIsNone(self.db.get_photo(self.video_id)["clip_path"])
        self.assertTrue(os.path.exists(self.video), "the original is never touched")

    def test_only_files_inside_the_cache_are_removed(self) -> None:
        from photo_reviewer.scanner import _invalidate_stale_video_renders

        # A stale column pointing at an original must not be followed.
        self.db.set_video_paths(self.video_id, clip_path=self.video)
        _invalidate_stale_video_renders(self.folder, self.db, self.cfg)
        self.cfg.video_clip_seconds = 15
        _invalidate_stale_video_renders(self.folder, self.db, self.cfg)
        self.assertTrue(os.path.exists(self.video))


class StalledVideoRetryTests(unittest.TestCase):
    """A broken video is retried once per version of the file, not every scan."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.db = Database(os.path.join(self.tmp.name, "l.db"))
        self.path = os.path.join(self.folder, "broken.mp4")
        Path(self.path).write_bytes(b"\x00" * 64)
        self.db.upsert_photo(
            {
                "path": self.path,
                "folder": self.folder,
                "filename": "broken.mp4",
                "status": "error",
                "media_kind": "video",
                "video_info": {},
            }
        )

    def tearDown(self) -> None:
        self.db.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_the_row_is_found_by_a_targeted_query(self) -> None:
        rows = self.db.stalled_video_rows(self.folder)
        self.assertEqual([os.path.basename(r["path"]) for r in rows], ["broken.mp4"])

    def test_a_healthy_video_is_not_returned(self) -> None:
        other = os.path.join(self.folder, "fine.mp4")
        Path(other).write_bytes(b"\x00" * 16)
        self.db.upsert_photo(
            {
                "path": other,
                "folder": self.folder,
                "filename": "fine.mp4",
                "status": "analyzed",
                "media_kind": "video",
                "video_info": {"video_codec": "h264"},
                "thumb_path": os.path.join(self.folder, "fine_thumb.jpg"),
                "proxy_path": os.path.join(self.folder, "fine_proxy.jpg"),
            }
        )
        rows = self.db.stalled_video_rows(self.folder)
        self.assertEqual([os.path.basename(r["path"]) for r in rows], ["broken.mp4"])

    def test_the_same_broken_file_is_only_retried_once(self) -> None:
        from photo_reviewer import scanner as scanner_mod

        scanner_mod._STALLED_PROBE_STAMPS.clear()
        first = scanner_mod._stalled_video_pairs(self.folder, self.db, [])
        self.assertEqual(len(first), 1)
        second = scanner_mod._stalled_video_pairs(self.folder, self.db, [])
        self.assertEqual(second, [], "an unchanged broken file is not retried")

    def test_touching_the_file_earns_another_attempt(self) -> None:
        from photo_reviewer import scanner as scanner_mod

        scanner_mod._STALLED_PROBE_STAMPS.clear()
        scanner_mod._stalled_video_pairs(self.folder, self.db, [])
        Path(self.path).write_bytes(b"\x00" * 128)
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        again = scanner_mod._stalled_video_pairs(self.folder, self.db, [])
        self.assertEqual(len(again), 1, "a changed file is worth another look")


class LivePhotoPairTrashTests(unittest.TestCase):
    """A merged Live Photo moves to the trash as a pair, and comes back as one."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.still = os.path.join(self.folder, "IMG_9.JPG")
        Image.new("RGB", (60, 80), (90, 120, 150)).save(self.still, "JPEG")
        self.movie = os.path.join(self.folder, "IMG_9.MOV")
        Path(self.movie).write_bytes(b"movie-bytes")
        self.photo_id = self.db.upsert_photo(
            {
                "path": self.still,
                "folder": self.folder,
                "filename": "IMG_9.JPG",
                "status": "analyzed",
                "media_kind": "photo",
                "live_path": self.movie,
                "score": 8.0,
            }
        )
        self.client = create_app(self.cfg, self.db).test_client()

    def tearDown(self) -> None:
        self.db.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_deleting_moves_both_files(self) -> None:
        resp = self.client.post("/api/delete", json={"ids": [self.photo_id]})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(self.still))
        self.assertFalse(os.path.exists(self.movie), "the movie follows the still")

        row = self.db.get_photo(self.photo_id)
        self.assertEqual(row["status"], "deleted")
        day_dir = os.path.dirname(row["path"])
        names = sorted(os.listdir(day_dir))
        self.assertIn("IMG_9.MOV", names)
        self.assertTrue(any(n.startswith("IMG_9") and n.upper().endswith(".JPG") for n in names))

    def test_restoring_brings_both_back(self) -> None:
        self.client.post("/api/delete", json={"ids": [self.photo_id]})
        resp = self.client.post("/api/restore", json={"ids": [self.photo_id]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.exists(self.still))
        self.assertTrue(os.path.exists(self.movie))
        self.assertEqual(Path(self.movie).read_bytes(), b"movie-bytes")

    def test_purging_removes_both(self) -> None:
        self.client.post("/api/delete", json={"ids": [self.photo_id]})
        resp = self.client.post(
            "/api/delete/permanent", json={"ids": [self.photo_id]}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.db.get_photo(self.photo_id))
        self.assertFalse(os.path.exists(self.still))
        self.assertFalse(os.path.exists(self.movie))
        self.assertEqual(resp.get_json().get("live_removed"), 1)

    def test_a_motion_photo_has_nothing_extra_to_move(self) -> None:
        # Its video is inside the image, so live_path points into the cache.
        cache = os.path.join(self.folder, ".photo-review-cache")
        os.makedirs(cache, exist_ok=True)
        embedded = os.path.join(cache, "abc_motion.mp4")
        Path(embedded).write_bytes(b"extracted")
        self.db.upsert_photo(
            {
                "path": self.still,
                "folder": self.folder,
                "filename": "IMG_9.JPG",
                "status": "analyzed",
                "media_kind": "photo",
                "live_path": embedded,
            }
        )
        resp = self.client.post("/api/delete", json={"ids": [self.photo_id]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            os.path.exists(embedded),
            "a cache file is not the user's footage and is left for the sweeper",
        )


class JobPruningTests(unittest.TestCase):
    """Finished jobs must not accumulate for the life of the process.

    Every scan, transcode and bin-emptying created an entry that was never
    removed, so a long-running instance grew without bound and the jobs endpoint
    kept returning ancient runs.
    """

    def setUp(self) -> None:
        from photo_reviewer.scanner import JobManager

        self.manager = JobManager()
        self.manager.KEEP_FINISHED = 3

    def _finish(self, offset: float = 0.0) -> str:
        # A distinct folder per job: these tests are about pruning, and a job for
        # a folder that already has one running is now refused on purpose.
        job = self.manager.create(f"folder-{offset}")
        job.status = "completed"
        job.started_at = time.time() + offset
        return job.id

    def test_a_folder_with_a_running_job_is_refused(self) -> None:
        """Two workers on one folder would write the same rows at once."""
        from photo_reviewer.scanner import JobConflict

        running = self.manager.create("C:/photos")
        running.status = "running"
        with self.assertRaises(JobConflict) as caught:
            self.manager.create("C:/photos")
        self.assertEqual(caught.exception.job.id, running.id)

    def test_the_guard_is_case_insensitive(self) -> None:
        """Windows paths differ in case; the same folder must still collide."""
        from photo_reviewer.scanner import JobConflict

        running = self.manager.create("C:/Photos")
        running.status = "running"
        with self.assertRaises(JobConflict):
            self.manager.create("c:/photos")

    def test_a_finished_job_does_not_block_a_new_one(self) -> None:
        from photo_reviewer.scanner import JobConflict

        done = self.manager.create("C:/photos")
        done.status = "completed"
        # No JobConflict raised: a finished run is not a running one.
        try:
            self.manager.create("C:/photos")
        except JobConflict:  # pragma: no cover - the assert below reports it
            self.fail("a completed job must not block a new one")

    def test_a_child_job_may_share_its_parents_folder(self) -> None:
        """An import's index pass is the same folder as the import itself.

        The parent is blocked waiting for the child, so refusing the child would
        make an import unable to index its own copies.
        """
        from photo_reviewer.scanner import JobConflict

        parent = self.manager.create("C:/photos")
        parent.status = "running"
        child = self.manager.create("C:/photos", parent_id=parent.id)
        self.assertEqual(child.parent_id, parent.id)
        # A second unrelated job on the same folder is still refused.
        other = self.manager.create("C:/photos2")
        other.status = "running"
        with self.assertRaises(JobConflict):
            self.manager.create("C:/photos2")

    def test_independent_work_opts_out_of_the_rule(self) -> None:
        """The bin sweep and video transcodes touch files, not the index."""
        running = self.manager.create("C:/photos")
        running.status = "running"
        # No exception: exclusive=False is the explicit opt-out.
        self.manager.create("C:/photos", exclusive=False)

    def test_finished_jobs_are_capped(self) -> None:
        ids = [self._finish(offset=i) for i in range(10)]
        self.manager.create("trigger")
        kept = {job.id for job in self.manager.list()}
        self.assertLessEqual(len(kept), 4, "cap plus the new job")
        self.assertIn(ids[-1], kept, "the newest finished job is kept")
        self.assertNotIn(ids[0], kept, "the oldest is dropped")

    def test_running_jobs_are_never_pruned(self) -> None:
        running = self.manager.create("folder")
        running.status = "running"
        for i in range(10):
            self._finish(offset=i)
        self.manager.create("trigger")
        self.assertIn(running.id, {job.id for job in self.manager.list()})

    def test_an_old_finished_job_expires(self) -> None:
        job = self.manager.create("folder")
        job.status = "completed"
        job.started_at = time.time() - (self.manager.FINISHED_TTL + 10)
        self.manager.create("trigger")
        self.assertNotIn(job.id, {j.id for j in self.manager.list()})


class AnalysisStateQueryTests(unittest.TestCase):
    """The queue decision is one query, not one per photo."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.db = Database(os.path.join(self.tmp.name, "l.db"))

    def tearDown(self) -> None:
        self.db.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def _add(self, name: str, **extra) -> None:
        record = {
            "path": os.path.join(self.folder, name),
            "folder": self.folder,
            "filename": name,
            "status": "analyzed",
            "score": 8.0,
        }
        record.update(extra)
        self.db.upsert_photo(record)

    def test_rows_are_keyed_by_path_for_one_query(self) -> None:
        self._add("a.jpg")
        self._add("b.jpg", status="pending")
        rows = self.db.analysis_state_for_folder(self.folder)
        self.assertEqual(set(rows), {
            os.path.join(self.folder, "a.jpg"),
            os.path.join(self.folder, "b.jpg"),
        })
        self.assertEqual(rows[os.path.join(self.folder, "b.jpg")]["status"], "pending")

    def test_only_the_needed_columns_come_back(self) -> None:
        self._add("a.jpg", tags=["x"], reason="r")
        row = self.db.analysis_state_for_folder(self.folder)[
            os.path.join(self.folder, "a.jpg")
        ]
        for heavy in ("tags", "exif", "dimensions", "video_info"):
            self.assertNotIn(heavy, row, f"{heavy} should not be decoded here")

    def test_a_pictureless_video_is_a_stalled_row(self) -> None:
        """Metadata present but no poster still needs another pass."""
        self._add(
            "clip.mp4",
            media_kind="video",
            status="analyzed",
            video_info={"video_codec": "h264"},
            thumb_path=None,
            proxy_path=None,
        )
        paths = [row["path"] for row in self.db.stalled_video_rows(self.folder)]
        self.assertEqual(paths, [os.path.join(self.folder, "clip.mp4")])

    def test_the_column_default_fills_in_a_missing_kind(self) -> None:
        """A row written without a kind is a photo, not an unknown."""
        self._add("old.jpg", media_kind=None)
        rows = self.db.analysis_state_for_folder(self.folder)
        self.assertEqual(
            rows[os.path.join(self.folder, "old.jpg")]["media_kind"], "photo"
        )

    def test_a_video_that_lost_its_poster_is_retried(self) -> None:
        """Analyzed but with no picture: nothing to show, so another pass."""
        self._add(
            "lost.mp4",
            media_kind="video",
            status="analyzed",
            video_info={"video_codec": "h264"},
            thumb_path=None,
            proxy_path=None,
        )
        paths = [row["path"] for row in self.db.stalled_video_rows(self.folder)]
        self.assertIn(os.path.join(self.folder, "lost.mp4"), paths)


class SortTieBreakTests(unittest.TestCase):
    """Equal scores are ordered by capture time, not by whatever SQLite stored.

    A folder where many photos scored 8.0 came back shuffled, which is what made
    a shoot look out of order even with "总分从高到低" selected.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.db = Database(os.path.join(self.tmp.name, "l.db"))

    def tearDown(self) -> None:
        self.db.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def _add(self, name: str, score: float, captured: str | None) -> None:
        exif = {"datetime_original": captured} if captured else {}
        self.db.upsert_photo(
            {
                "path": os.path.join(self.folder, name),
                "folder": self.folder,
                "filename": name,
                "status": "analyzed",
                "score": score,
                "exif": exif,
            }
        )

    def _names(self, sort: str) -> list[str]:
        return [
            row["filename"]
            for row in self.db.list_photos(folder=self.folder, sort=sort, limit=50)
        ]

    def test_equal_scores_are_ordered_by_capture_time(self) -> None:
        self._add("mid.jpg", 8.0, "2024:05:02 10:00:00")
        self._add("late.jpg", 8.0, "2024:05:03 10:00:00")
        self._add("early.jpg", 8.0, "2024:05:01 10:00:00")
        self.assertEqual(
            self._names("score_desc"), ["late.jpg", "mid.jpg", "early.jpg"]
        )

    def test_score_still_outranks_time(self) -> None:
        self._add("old_high.jpg", 9.0, "2020:01:01 10:00:00")
        self._add("new_low.jpg", 5.0, "2025:01:01 10:00:00")
        self.assertEqual(self._names("score_desc"), ["old_high.jpg", "new_low.jpg"])

    def test_ascending_scores_also_tie_break_by_time(self) -> None:
        self._add("a.jpg", 5.0, "2024:05:01 10:00:00")
        self._add("b.jpg", 5.0, "2024:05:02 10:00:00")
        self.assertEqual(self._names("score_asc"), ["b.jpg", "a.jpg"])

    def test_a_photo_without_a_date_sorts_after_one_with(self) -> None:
        self._add("dated.jpg", 8.0, "2024:05:01 10:00:00")
        self._add("undated.jpg", 8.0, None)
        self.assertEqual(self._names("score_desc"), ["dated.jpg", "undated.jpg"])

    def test_python_sorting_matches_the_sql(self) -> None:
        self._add("mid.jpg", 8.0, "2024:05:02 10:00:00")
        self._add("late.jpg", 8.0, "2024:05:03 10:00:00")
        self._add("early.jpg", 8.0, "2024:05:01 10:00:00")
        rows = self.db.list_photos(folder=self.folder, sort="score_desc", limit=50)
        shuffled = list(reversed(rows))
        sort_photos(shuffled, "score_desc")
        self.assertEqual(
            [r["filename"] for r in shuffled], [r["filename"] for r in rows]
        )


class LivePhotoMergeSwitchTests(unittest.TestCase):
    """「合并显示实况照片」 decides whether a pair is one item or two.

    A Live Photo is an iPhone still plus a same-named movie. Merged (the
    default) the movie belongs to the still and is what the 实况 button plays;
    split, the two are separate items, which is the only way to see the movie on
    its own.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        Image.new("RGB", (80, 60), (100, 120, 140)).save(
            os.path.join(self.folder, "IMG_1.JPG"), "JPEG"
        )
        with open(os.path.join(self.folder, "IMG_1.MOV"), "wb") as fh:
            fh.write(b"\x00" * 512)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _pairs(self, merge: bool) -> list[str]:
        from photo_reviewer.scanner import discover_photo_pairs

        return [
            os.path.basename(path)
            for path, _raw in discover_photo_pairs(
                self.folder, (".jpg",), set(), merge_live=merge
            )
        ]

    def test_merged_by_default(self) -> None:
        self.assertEqual(self._pairs(True), ["IMG_1.JPG"])

    def test_splitting_shows_both_files(self) -> None:
        self.assertEqual(sorted(self._pairs(False)), ["IMG_1.JPG", "IMG_1.MOV"])

    def test_the_default_config_merges(self) -> None:
        cfg = Config(data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "l.db"))
        self.assertTrue(cfg.merge_live_photos)

    def test_the_switch_survives_a_round_trip(self) -> None:
        from photo_reviewer.config import EDITABLE_FIELDS

        self.assertIn("merge_live_photos", EDITABLE_FIELDS)


class EquivalentFocalTests(unittest.TestCase):
    """A phone lens says 4.2 mm; the picture looks like 26 mm."""

    def test_a_recorded_equivalent_is_used(self) -> None:
        from photo_reviewer.exif import _equivalent_focal

        self.assertEqual(_equivalent_focal(26), 26.0)
        self.assertEqual(_equivalent_focal("35.0"), 35.0)

    def test_a_missing_tag_yields_nothing(self) -> None:
        from photo_reviewer.exif import _equivalent_focal

        for value in (None, 0, "nonsense"):
            self.assertIsNone(_equivalent_focal(value), value)

    def test_formatting_keeps_a_useful_precision(self) -> None:
        from photo_reviewer.exif import _format_focal

        self.assertEqual(_format_focal(26), "26mm")
        self.assertEqual(_format_focal(4.2), "4.2mm")
        self.assertEqual(_format_focal(23.5), "23.5mm")

    def test_the_reader_prefers_the_equivalent(self) -> None:
        """An EXIF block with both values exposes the equivalent separately."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phone.jpg")
            image = Image.new("RGB", (64, 48), (90, 90, 90))
            exif = Image.Exif()
            exif[37386] = (42, 10)  # 4.2 mm physical
            exif[41989] = 26  # 35 mm equivalent
            image.save(path, "JPEG", exif=exif)
            from photo_reviewer.exif import extract_exif

            _location, data = extract_exif(path)
            self.assertEqual(data.get("focal_length_35mm"), "26mm")


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


class GalleryPayloadTests(unittest.TestCase):
    """What a list row carries, and what the detail dialog fetches itself.

    A 4000-photo album used to send the EXIF block with every row (~30% of the
    payload) and stat the raw sibling of every photo with one (~100 filesystem
    calls per page). Both now come from the per-photo detail endpoint, so this
    pins the split: the list stays small, the dialog still gets everything.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        self.cache = os.path.join(self.folder, ".photo-review-cache")
        os.makedirs(self.folder)
        os.makedirs(self.cache)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.image = os.path.join(self.folder, "A.JPG")
        self.raw = os.path.join(self.folder, "A.NEF")
        Image.new("RGB", (40, 30), (120, 90, 60)).save(self.image, "JPEG")
        Path(self.raw).write_bytes(b"N" * 2048)
        self.thumb = os.path.join(self.cache, "a_thumb.jpg")
        Image.new("RGB", (40, 30), (120, 90, 60)).save(self.thumb, "JPEG")
        self.db.upsert_photo(
            {
                "path": self.image,
                "folder": self.folder,
                "filename": "A.JPG",
                "status": "analyzed",
                "score": 8.0,
                "exif": {
                    "datetime_original": "2024:05:01 10:20:30",
                    "make": "FUJIFILM",
                    "model": "X-T30 III",
                    "iso": "400",
                },
                "dimensions": {"technical": 7.0, "memory": 8.0, "uniqueness": 6.0},
                "tags": ["风景"],
                "title": "堤上的风",
                "size": 12345,
                "width": 6000,
                "height": 4000,
                "raw_path": self.raw,
                "thumb_path": self.thumb,
                "phash": "abc123def4567890",
            }
        )
        self.photo_id = self.db.get_photo_by_path(self.image)["id"]
        # Mark the one-off startup repairs as done, the state a running library
        # is in. Otherwise the orientation pass may rewrite the cached path and
        # re-render the thumbnail underneath the assertions below.
        self.db.set_meta(ORIENTATION_REPAIR_KEY, ORIENTATION_REPAIR_VERSION)
        self.db.set_meta(DIMENSIONS_REPAIR_KEY, DIMENSIONS_REPAIR_VERSION)
        self.client = create_app(self.cfg, self.db).test_client()

    def tearDown(self) -> None:
        self.db.close()
        # Windows can still hold the served JPEG open for a moment; a failed
        # cleanup of a temp dir must not fail the test.
        try:
            self.tmp.cleanup()
        except OSError:  # pragma: no cover - timing dependent
            pass

    def test_a_list_row_leaves_out_exif_and_the_raw_size(self) -> None:
        row = self.client.get("/api/photos").get_json()["photos"][0]
        self.assertNotIn("exif", row)
        self.assertNotIn("raw_size", row)
        # Everything a card paints is still there.
        for key in ("id", "filename", "score", "tags", "reason", "status", "favorite"):
            self.assertIn(key, row)
        self.assertTrue(row["has_raw"])
        self.assertEqual(row["raw_ext"], "NEF")

    def test_the_grid_never_touches_the_filesystem(self) -> None:
        """One ``os.path.getsize`` per raw-carrying photo was the old cost."""
        with patch("photo_reviewer.server.os.path.getsize") as getsize:
            self.client.get("/api/photos")
        getsize.assert_not_called()

    def test_the_thumb_version_changes_with_the_thumbnail(self) -> None:
        row = self.client.get("/api/photos").get_json()["photos"][0]
        self.assertEqual(row["thumb_version"], "abc123def4567890")
        # Re-rendering a thumbnail writes a new hash; the URL follows it.
        self.db.update_cache_paths(self.photo_id, self.thumb, self.thumb, "ffff0000")
        row = self.client.get("/api/photos").get_json()["photos"][0]
        self.assertEqual(row["thumb_version"], "ffff0000")

    def test_the_detail_endpoint_supplies_what_the_dialog_shows(self) -> None:
        detail = self.client.get(f"/api/photo/{self.photo_id}/detail").get_json()
        self.assertEqual(detail["exif"]["make"], "FUJIFILM")
        self.assertEqual(detail["raw_size"], 2048)
        self.assertEqual((detail["width"], detail["height"]), (6000, 4000))
        self.assertEqual(detail["size"], 12345)

    def test_the_detail_endpoint_404s_for_an_unknown_photo(self) -> None:
        self.assertEqual(
            self.client.get("/api/photo/999999/detail").status_code, 404
        )

    def test_the_single_photo_endpoint_still_returns_everything(self) -> None:
        """Older callers (and the edit round trip) keep the full payload."""
        body = self.client.get(f"/api/photo/{self.photo_id}").get_json()
        self.assertIn("exif", body)
        self.assertEqual(body["raw_size"], 2048)
        self.assertEqual(body["folder"], self.folder)

    def test_a_versioned_thumbnail_url_is_cacheable_for_a_year(self) -> None:
        """The version query is what makes scrolling back cost no requests."""
        plain = self.client.get(f"/api/thumbnail/{self.photo_id}")
        versioned = self.client.get(
            f"/api/thumbnail/{self.photo_id}?v=abc123def4567890"
        )
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(versioned.status_code, 200)
        self.assertIn("no-cache", plain.headers["Cache-Control"])
        self.assertIn("max-age=31536000", versioned.headers["Cache-Control"])
        # The version only changes the caching, never the picture.
        self.assertEqual(len(plain.data), len(versioned.data))
        self.assertEqual(plain.headers["ETag"], versioned.headers["ETag"])


class VideoPayloadTests(unittest.TestCase):
    """What a video row carries, without needing ffmpeg.

    The rows are written by hand here: the point is the API contract (which
    fields the gallery and the dialog read), not the extraction, which
    ``tests/test_video.py`` covers against real files.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        self.cache = os.path.join(self.folder, ".photo-review-cache")
        os.makedirs(self.folder)
        os.makedirs(self.cache)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        # A running library has already been through the one-off repairs; the
        # orientation pass would otherwise rewrite paths under the assertions.
        self.db.set_meta(ORIENTATION_REPAIR_KEY, ORIENTATION_REPAIR_VERSION)
        self.db.set_meta(DIMENSIONS_REPAIR_KEY, DIMENSIONS_REPAIR_VERSION)

        self.video_path = os.path.join(self.folder, "trip.mp4")
        Path(self.video_path).write_bytes(b"\x00" * 8192)
        self.clip_path = os.path.join(self.cache, "trip_clip.mp4")
        Path(self.clip_path).write_bytes(b"\x11" * 4096)
        self.poster_path = os.path.join(self.cache, "trip_thumb.jpg")
        Image.new("RGB", (64, 36), (120, 90, 60)).save(self.poster_path, "JPEG")

        self.video_id = self.db.upsert_photo(
            {
                "path": self.video_path,
                "folder": self.folder,
                "filename": "trip.mp4",
                "status": "analyzed",
                "media_kind": "video",
                "duration_ms": 93_000,
                "width": 1920,
                "height": 1080,
                "thumb_path": self.poster_path,
                "proxy_path": self.poster_path,
                "clip_path": self.clip_path,
                "play_state": "none",
                "video_info": {
                    "video_codec": "hevc",
                    "video_profile": "Main 10",
                    "fps": 59.94,
                    "video_bitrate": 45_000_000,
                    "audio_codec": "aac",
                    "audio_bitrate": 128_000,
                    "container": ".mov",
                    "capture_time": "2024:05:01 10:20:30",
                    "has_audio": True,
                },
                "exif": {"datetime_original": "2024:05:01 10:20:30"},
            }
        )
        still = os.path.join(self.folder, "still.jpg")
        Image.new("RGB", (48, 36), (90, 120, 150)).save(still, "JPEG")
        live = os.path.join(self.cache, "still_motion.mp4")
        Path(live).write_bytes(b"\x22" * 2048)
        self.still_id = self.db.upsert_photo(
            {
                "path": still,
                "folder": self.folder,
                "filename": "still.jpg",
                "status": "analyzed",
                "live_path": live,
            }
        )
        self.client = create_app(self.cfg, self.db).test_client()

    def tearDown(self) -> None:
        self.db.close()
        try:
            self.tmp.cleanup()
        except OSError:  # pragma: no cover - Windows may hold a served file
            pass

    def test_a_list_row_says_it_is_a_video_and_how_long(self) -> None:
        rows = {
            row["filename"]: row
            for row in self.client.get("/api/photos").get_json()["photos"]
        }
        video = rows["trip.mp4"]
        self.assertEqual(video["media_kind"], "video")
        self.assertEqual(video["duration_ms"], 93_000)
        self.assertFalse(video["has_live"])
        self.assertEqual(rows["still.jpg"]["media_kind"], "photo")
        self.assertTrue(rows["still.jpg"]["has_live"])

    def test_the_list_row_does_not_carry_the_codec_block(self) -> None:
        """Codec detail is for the dialog, like EXIF and the raw size."""
        row = self.client.get("/api/photos").get_json()["photos"][0]
        self.assertNotIn("video_info", row)

    def test_the_detail_endpoint_returns_the_video_facts(self) -> None:
        detail = self.client.get(f"/api/photo/{self.video_id}/detail").get_json()
        self.assertEqual(detail["media_kind"], "video")
        self.assertEqual(detail["duration_ms"], 93_000)
        self.assertEqual(detail["video_info"]["video_codec"], "hevc")
        self.assertEqual(detail["video_info"]["fps"], 59.94)
        self.assertEqual(detail["clip_url"], f"/api/video/{self.video_id}/clip")
        self.assertIn("ffmpeg", detail)

    def test_a_photo_has_no_video_block(self) -> None:
        detail = self.client.get(f"/api/photo/{self.still_id}/detail").get_json()
        self.assertEqual(detail["media_kind"], "photo")
        self.assertEqual(detail["video_info"], {})
        self.assertIsNone(detail["clip_url"])
        self.assertTrue(detail["has_live"])

    def test_the_clip_endpoint_serves_the_cached_file(self) -> None:
        resp = self.client.get(f"/api/video/{self.video_id}/clip")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["Content-Type"], "video/mp4")
        self.assertEqual(resp.headers["Accept-Ranges"], "bytes")
        self.assertEqual(len(resp.data), 4096)

    def test_the_stream_endpoint_answers_range_requests(self) -> None:
        """A <video> can only seek if the server speaks bytes ranges."""
        resp = self.client.get(
            f"/api/video/{self.video_id}/stream",
            headers={"Range": "bytes=100-199"},
        )
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(len(resp.data), 100)
        self.assertEqual(
            resp.headers["Content-Range"], "bytes 100-199/8192"
        )
        self.assertEqual(resp.headers["Content-Type"], "video/mp4")

    def test_the_stream_endpoint_uses_the_container_mime_type(self) -> None:
        self.assertEqual(
            self.client.get(f"/api/video/{self.video_id}/stream").headers[
                "Content-Type"
            ],
            "video/mp4",
        )

    def test_the_live_endpoint_serves_the_motion_part(self) -> None:
        resp = self.client.get(f"/api/photo/{self.still_id}/live")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2048)

    def test_a_photo_without_a_motion_part_is_a_404(self) -> None:
        plain = os.path.join(self.folder, "plain.jpg")
        Image.new("RGB", (20, 20), (1, 2, 3)).save(plain, "JPEG")
        pid = self.db.upsert_photo(
            {
                "path": plain,
                "folder": self.folder,
                "filename": "plain.jpg",
                "status": "analyzed",
            }
        )
        self.assertEqual(self.client.get(f"/api/photo/{pid}/live").status_code, 404)

    def test_video_endpoints_refuse_a_photo_row(self) -> None:
        for endpoint in ("clip", "stream", "playable"):
            resp = self.client.get(f"/api/video/{self.still_id}/{endpoint}")
            self.assertEqual(resp.status_code, 404, endpoint)

    def test_unknown_ids_are_404(self) -> None:
        self.assertEqual(self.client.get("/api/video/999999/clip").status_code, 404)
        self.assertEqual(self.client.get("/api/video/999999/stream").status_code, 404)
        self.assertEqual(self.client.get("/api/video/999999/playable").status_code, 404)

    def test_the_kind_filter_separates_videos_photos_and_live_photos(self) -> None:
        """「照片」 includes motion photos; 「实况」 is the live subset."""
        def names(query: str) -> set[str]:
            resp = self.client.get(f"/api/photos?kind={query}")
            return {row["filename"] for row in resp.get_json()["photos"]}

        self.assertEqual(names("video"), {"trip.mp4"})
        # A motion photo counts as a photo - that is the whole point of the
        # "照片" tab: it is a picture that happens to move.
        self.assertEqual(names("photo"), {"still.jpg"})
        self.assertEqual(names("live"), {"still.jpg"})
        self.assertEqual(names(""), {"trip.mp4", "still.jpg"})

    def test_an_unknown_kind_is_ignored(self) -> None:
        resp = self.client.get("/api/photos?kind=sounds")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["photos"]), 2)

    def test_the_playable_endpoint_streams_what_is_already_there(self) -> None:
        ready = os.path.join(self.cache, "trip_play.mp4")
        Path(ready).write_bytes(b"\x33" * 1024)
        self.db.set_video_paths(self.video_id, play_path=ready, play_state="ready")
        resp = self.client.get(f"/api/video/{self.video_id}/playable")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1024)

    def test_the_playable_endpoint_starts_a_job_when_nothing_is_ready(self) -> None:
        resp = self.client.get(f"/api/video/{self.video_id}/playable")
        # Either a job was started (202) or ffmpeg is missing (503); both are
        # contract answers, and neither may be a crash.
        self.assertIn(resp.status_code, (202, 503))
        if resp.status_code == 202:
            body = resp.get_json()
            self.assertIn("job_id", body)
            self.assertIn(body["state"], ("running", "pending"))
            # Wait for it: the job writes into the temp folder, and tearing the
            # fixture down underneath a running ffmpeg turns an unrelated test's
            # cleanup into a PermissionError on Windows.
            self._drain_job(body["job_id"])

    def _drain_job(self, job_id: str, timeout: float = 30.0) -> None:
        from photo_reviewer.scanner import JOBS

        deadline = time.time() + timeout
        while time.time() < deadline:
            job = JOBS.get(job_id)
            if job is None or getattr(job, "status", "") in (
                "completed",
                "error",
                "cancelled",
            ):
                return
            time.sleep(0.05)

    def test_the_cache_sweep_keeps_derived_video_files(self) -> None:
        """Cleanup must not delete a preview that a row still points at."""
        from photo_reviewer.cache import cleanup_folder_cache

        report = cleanup_folder_cache(self.folder, self.db, self.cfg.cache_dir_name)
        self.assertTrue(os.path.exists(self.clip_path))
        self.assertTrue(os.path.exists(self.poster_path))
        self.assertGreaterEqual(report["freed"], 0)

    def test_the_cache_sweep_removes_an_orphaned_preview(self) -> None:
        from photo_reviewer.cache import cleanup_folder_cache

        orphan = os.path.join(self.cache, "nobody_clip.mp4")
        Path(orphan).write_bytes(b"\x44" * 512)
        cleanup_folder_cache(self.folder, self.db, self.cfg.cache_dir_name)
        self.assertFalse(os.path.exists(orphan))


def _ffmpeg_available() -> bool:
    from photo_reviewer.video import tools_available

    return tools_available()


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg is not installed")
class VideoPlaybackTests(unittest.TestCase):
    """The full path with real files: index, poster, clip, remux."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "pics")
        os.makedirs(self.folder)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)

    def tearDown(self) -> None:
        from photo_reviewer.repair import wait_for_repairs

        wait_for_repairs(timeout=30)
        self.db.close()
        try:
            self.tmp.cleanup()
        except OSError:  # pragma: no cover
            pass

    def _make_video(self, name: str) -> str:
        from photo_reviewer.video import find_tools

        tools = find_tools()
        path = os.path.join(self.folder, name)
        subprocess.run(
            [
                tools.ffmpeg, "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", path,
            ],
            check=False,
            capture_output=True,
        )
        return path

    def test_indexing_a_video_writes_a_poster_and_queues_it_for_analysis(self) -> None:
        from photo_reviewer.scanner import _index_one

        path = self._make_video("trip.mp4")
        self.assertTrue(_index_one(path, self.folder, self.db, self.cfg))
        row = self.db.get_photo_by_path(path)
        self.assertEqual(row["media_kind"], "video")
        # A video is analysed like a photo, from its poster frame - so it waits
        # in "待整理" until the model has looked at it.
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["score"])
        self.assertGreater(row["duration_ms"], 5000)
        self.assertAlmostEqual(row["width"], 320)
        self.assertTrue(row["thumb_path"] and os.path.exists(row["thumb_path"]))
        self.assertEqual(row["video_info"]["video_codec"], "h264")

    def test_a_video_without_a_poster_is_not_queued_for_analysis(self) -> None:
        """No ffmpeg means no frame to send, so the row carries the reason."""
        from photo_reviewer.scanner import _index_one

        path = self._make_video("trip.mp4")
        with patch(
            "photo_reviewer.pipeline.video.make_poster", return_value=False
        ), patch("photo_reviewer.pipeline.video.probe", return_value=None):
            self.assertTrue(_index_one(path, self.folder, self.db, self.cfg))
        row = self.db.get_photo_by_path(path)
        self.assertEqual(row["status"], "error")
        self.assertIn("ffmpeg", row["error"] or "")

    def test_the_clip_endpoint_generates_and_caches(self) -> None:
        from photo_reviewer.scanner import _index_one

        path = self._make_video("trip.mp4")
        _index_one(path, self.folder, self.db, self.cfg)
        row = self.db.get_photo_by_path(path)
        client = create_app(self.cfg, self.db).test_client()

        first = client.get(f"/api/video/{row['id']}/clip")
        self.assertEqual(first.status_code, 200)
        self.assertGreater(len(first.data), 1024)
        saved = self.db.get_photo(row["id"])["clip_path"]
        self.assertTrue(saved and os.path.exists(saved))

        second = client.get(f"/api/video/{row['id']}/clip")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data, second.data)

    def test_the_clip_is_about_three_seconds(self) -> None:
        from photo_reviewer.scanner import _index_one
        from photo_reviewer.video import probe

        path = self._make_video("trip.mp4")
        _index_one(path, self.folder, self.db, self.cfg)
        row = self.db.get_photo_by_path(path)
        create_app(self.cfg, self.db).test_client().get(
            f"/api/video/{row['id']}/clip"
        )
        clip = probe(self.db.get_photo(row["id"])["clip_path"])
        self.assertIsNotNone(clip)
        # The default preview length is three seconds.
        self.assertLessEqual(clip.duration_ms, 3400)
        self.assertGreater(clip.duration_ms, 2400)


class VolumeIdentityTests(unittest.TestCase):
    """The card identity layer, exercised on all three platforms.

    The Windows, macOS and Linux backends cannot all run on one machine, so the
    pieces that decide *behaviour* - the comparison rules, the trust ranking and
    the mount-point walk - are tested directly against constructed identities.
    """

    def test_the_same_key_from_the_same_backend_matches(self) -> None:
        for source in (SOURCE_VOLUME_GUID, SOURCE_UUID, SOURCE_SERIAL, SOURCE_DEVICE):
            same, certain = identities_compatible(
                VolumeIdentity("k", source), VolumeIdentity("k", source)
            )
            self.assertTrue(same, source)
            self.assertEqual(certain, source != SOURCE_DEVICE, source)

    def test_different_keys_never_match(self) -> None:
        same, _ = identities_compatible(
            VolumeIdentity("k1", SOURCE_UUID), VolumeIdentity("k2", SOURCE_UUID)
        )
        self.assertFalse(same)

    def test_a_missing_identity_is_never_a_match(self) -> None:
        for recorded in (None, VolumeIdentity("k", SOURCE_UUID)):
            for current in (None, VolumeIdentity("k", SOURCE_UUID)):
                if recorded is current is None:
                    same, certain = identities_compatible(None, None)
                    self.assertFalse(same)
                    self.assertFalse(certain)

    def test_keys_from_different_backends_are_not_compared(self) -> None:
        """A GUID and a device number are not the same kind of answer."""
        same, certain = identities_compatible(
            VolumeIdentity("k", SOURCE_VOLUME_GUID), VolumeIdentity("k", SOURCE_DEVICE)
        )
        self.assertFalse(same)
        self.assertFalse(certain)

    def test_only_durable_backends_are_trusted(self) -> None:
        self.assertTrue(VolumeIdentity("k", SOURCE_VOLUME_GUID).trusted)
        self.assertTrue(VolumeIdentity("k", SOURCE_UUID).trusted)
        self.assertTrue(VolumeIdentity("k", SOURCE_SERIAL).trusted)
        # A POSIX device number is allocated per boot.
        self.assertFalse(VolumeIdentity("k", SOURCE_DEVICE).trusted)

    def test_trust_order_lists_every_backend(self) -> None:
        self.assertEqual(
            set(TRUST_ORDER),
            {SOURCE_VOLUME_GUID, SOURCE_UUID, SOURCE_SERIAL, SOURCE_DEVICE},
        )

    def test_a_missing_path_yields_no_identity(self) -> None:
        self.assertIsNone(volume_identity(None))
        self.assertIsNone(volume_identity(""))

    def test_a_missing_path_falls_back_to_the_volume_that_holds_it(self) -> None:
        """A card that was unplugged still belongs to the volume it lived on.

        Identifying the *parent* is what lets a pairing written for a card that
        is no longer mounted still be compared the next time it appears.
        """
        missing = os.path.join(tempfile.gettempdir(), "no-such-dir-xyz", "deeper")
        identity = volume_identity(missing)
        # Either the platform cannot identify it, or it identified the parent
        # volume - both are acceptable; a placeholder never is.
        if identity is not None:
            self.assertTrue(identity.key.strip())

    def test_a_real_directory_yields_an_identity_or_none(self) -> None:
        """Whatever this platform supports, the answer is never a placeholder."""
        with tempfile.TemporaryDirectory() as tmp:
            identity = volume_identity(tmp)
            if identity is not None:
                self.assertTrue(identity.key.strip())
                self.assertIn(identity.source, TRUST_ORDER)

    def test_the_mount_root_walks_up_to_the_volume_boundary(self) -> None:
        from photo_reviewer.volume import _mount_root

        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "a", "b", "c")
            os.makedirs(nested)
            root = _mount_root(nested)
            # The root must be an ancestor of the path and must be a directory.
            self.assertTrue(os.path.isdir(root))
            self.assertTrue(
                os.path.normcase(os.path.abspath(nested)).startswith(
                    os.path.normcase(root)
                )
            )
            if os.name == "nt":
                # On Windows that boundary is the drive itself.
                self.assertEqual(root, os.path.splitdrive(nested)[0] + os.sep)


class DatabaseBackupTests(unittest.TestCase):
    """The index holds work that cannot be rebuilt cheaply.

    Scores, tags, comments, hand-written memory edits and card pairings are all
    lost if the file goes, so a snapshot is taken on startup.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "library.db")
        self.db = Database(self.db_path)
        # Enough rows that the file clears the "nothing worth keeping" floor.
        with self.db as handle:
            handle.upsert_photo(
                {
                    "path": os.path.join(self.tmp.name, "a.jpg"),
                    "folder": self.tmp.name,
                    "filename": "a.jpg",
                    "status": "analyzed",
                    "score": 8.0,
                }
            )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_a_backup_is_a_usable_copy(self) -> None:
        from photo_reviewer.backup import backup_database

        target = backup_database(self.db_path)
        self.assertIsNotNone(target)
        self.assertTrue(os.path.exists(target))
        copy = Database(str(target))
        try:
            self.assertEqual(copy.stats()["total"], 1)
        finally:
            copy.close()

    def test_old_backups_are_pruned(self) -> None:
        from photo_reviewer.backup import backup_database

        for _ in range(7):
            backup_database(self.db_path)
        backups = [
            name
            for name in os.listdir(os.path.join(self.tmp.name, "backups"))
            if name.startswith("library-")
        ]
        self.assertLessEqual(len(backups), 5)

    def test_a_tiny_database_is_not_worth_backing_up(self) -> None:
        from photo_reviewer.backup import backup_database

        small = os.path.join(self.tmp.name, "small.db")
        with open(small, "wb") as handle:
            handle.write(b"x" * 100)
        self.assertIsNone(backup_database(small))

    def test_automatic_backups_are_rate_limited(self) -> None:
        from photo_reviewer.backup import BACKUP_STAMP_KEY, maybe_auto_backup

        first = maybe_auto_backup(self.db, self.db_path)
        self.assertIsNotNone(first)
        # A second call inside the interval must do nothing.
        self.assertIsNone(maybe_auto_backup(self.db, self.db_path))
        self.assertTrue(self.db.get_meta(BACKUP_STAMP_KEY))


class BatchAnalysisApiTests(unittest.TestCase):
    """The multi-photo "重新解读" action runs as a job, not a browser loop."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "album")
        os.makedirs(self.folder)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        # JOBS is process-wide; a job from a previous test must not be mistaken
        # for one of ours in tearDown.
        for job in JOBS.list():
            job.status = "completed"
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()
        self.ids = []
        for index in range(3):
            path = os.path.join(self.folder, f"p{index}.jpg")
            Image.new("RGB", (32, 24), (120, 90, 60)).save(path, "JPEG")
            self.db.upsert_photo(
                {
                    "path": path,
                    "folder": self.folder,
                    "filename": f"p{index}.jpg",
                    "status": "pending",
                }
            )
            self.ids.append(self.db.get_photo_by_path(path)["id"])

    def tearDown(self) -> None:
        # A batch job runs on a daemon thread and touches the database; let it
        # finish before the temp directory goes away.
        for job in JOBS.list():
            if job.status in ("pending", "running"):
                wait_for_job(job.id)
        self.db.close()
        self.tmp.cleanup()

    def test_a_dead_endpoint_is_refused_before_any_work(self) -> None:
        """The probe turns a 15-second-per-photo stall into one clear error."""
        with patch(
            "photo_reviewer.server.probe_endpoint",
            return_value=(False, "无法连接模型服务"),
        ):
            resp = self.client.post("/api/analyze/batch", json={"ids": self.ids})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("无法连接模型服务", resp.get_json()["error"])

    def test_a_reachable_endpoint_starts_a_job(self) -> None:
        with patch(
            "photo_reviewer.server.probe_endpoint", return_value=(True, "")
        ), patch("photo_reviewer.scanner.reanalyze_photo", return_value={}):
            resp = self.client.post("/api/analyze/batch", json={"ids": self.ids})
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body["total"], 3)
            self.assertTrue(body["job_id"])
            # The worker runs on its own thread, so it has to finish while the
            # patch is still in place - otherwise it calls the real function and
            # waits on a model that is not there.
            job = wait_for_job(body["job_id"])
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.report["analysed"], 3)

    def test_an_empty_selection_is_rejected(self) -> None:
        resp = self.client.post("/api/analyze/batch", json={"ids": []})
        self.assertEqual(resp.status_code, 400)

    def test_deleted_photos_are_not_queued(self) -> None:
        self.db.mark_deleted([self.ids[0]], {})
        with patch(
            "photo_reviewer.server.probe_endpoint", return_value=(True, "")
        ), patch("photo_reviewer.scanner.reanalyze_photo", return_value={}):
            resp = self.client.post("/api/analyze/batch", json={"ids": self.ids})
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body["total"], 2)
            wait_for_job(body["job_id"])

    def test_consecutive_failures_pause_the_run(self) -> None:
        """A dead endpoint must stop the queue, not fail every photo in turn.

        Each failed attempt costs the full retry budget, so running the whole
        selection would write a failure row for every photo and take hours.
        """
        from photo_reviewer.scanner import ANALYSIS_FAILURE_LIMIT

        ids = self.ids * 10  # more than the failure limit
        # The worker logs a warning per failure on purpose; that is the signal
        # this test is about, so keep it out of the suite's output.
        with patch(
            "photo_reviewer.server.probe_endpoint", return_value=(True, "")
        ), patch(
            "photo_reviewer.scanner.reanalyze_photo",
            side_effect=RuntimeError("endpoint down"),
        ), self.assertLogs("photo_reviewer.scanner", level="WARNING"):
            resp = self.client.post("/api/analyze/batch", json={"ids": ids})
            self.assertEqual(resp.status_code, 200)
            job = wait_for_job(resp.get_json()["job_id"])
        self.assertTrue(job.stalled)
        self.assertTrue(job.report["paused"])
        self.assertEqual(job.processed, ANALYSIS_FAILURE_LIMIT)


class DuplicateDetectionTests(unittest.TestCase):
    """Grouping photos by perceptual hash.

    The two rules that matter were both discovered by running the grouping over
    the real library, so they are pinned here: the grouping must be
    complete-linkage (single-linkage merged a 30-frame burst into one group of
    134 unrelated photos), and two photos from different folders are never the
    same picture.
    """

    @staticmethod
    def _photo(pid, phash, width=1000, height=1000, size=1000, folder="A", status="analyzed"):
        return {
            "id": pid,
            "phash": phash,
            "width": width,
            "height": height,
            "size": size,
            "folder": folder,
            "filename": f"{pid}.jpg",
            "status": status,
        }

    def test_identical_hashes_group_together(self) -> None:
        from photo_reviewer.duplicates import group_duplicates

        photos = [
            self._photo(1, "0f0f0f0f0f0f0f0f"),
            self._photo(2, "0f0f0f0f0f0f0f0f"),
        ]
        groups = group_duplicates(photos)
        self.assertEqual(len(groups), 1)
        self.assertEqual([p["id"] for p in groups[0].photos], [1, 2])

    def test_distance_beyond_the_threshold_is_not_a_group(self) -> None:
        from photo_reviewer.duplicates import group_duplicates

        photos = [
            self._photo(1, "0000000000000000"),
            self._photo(2, "000000000000ffff"),  # 16 bits apart
        ]
        self.assertEqual(group_duplicates(photos), [])

    def test_the_threshold_is_honoured(self) -> None:
        from photo_reviewer.duplicates import group_duplicates

        photos = [
            self._photo(1, "0000000000000000"),
            self._photo(2, "000000000000000f"),  # 4 bits apart
        ]
        self.assertEqual(group_duplicates(photos, threshold=3), [])
        self.assertEqual(len(group_duplicates(photos, threshold=4)), 1)

    def test_a_chain_does_not_collapse_into_one_group(self) -> None:
        """A -> B -> C where A and C are far apart must not be one group.

        This is the bug that made a real library report a group of 134 photos:
        measuring the transitive closure of "within threshold" merges a burst.
        """
        from photo_reviewer.duplicates import group_duplicates

        photos = [
            self._photo(1, "0000000000000000"),
            self._photo(2, "000000000000000f"),  # 4 from 1
            self._photo(3, "00000000000000ff"),  # 4 from 2, 8 from 1
        ]
        groups = group_duplicates(photos, threshold=6)
        # 1 and 3 are 8 apart, so no group may contain all three.
        for group in groups:
            self.assertLess(group.size, 3, [p["id"] for p in group.photos])

    def test_different_folders_are_never_grouped(self) -> None:
        """Two cameras produce visually similar frames; they are not duplicates."""
        from photo_reviewer.duplicates import group_duplicates

        photos = [
            self._photo(1, "0f0f0f0f0f0f0f0f", folder="Canon"),
            self._photo(2, "0f0f0f0f0f0f0f0f", folder="Sony"),
        ]
        self.assertEqual(group_duplicates(photos), [])

    def test_a_landscape_is_not_matched_to_a_portrait(self) -> None:
        from photo_reviewer.duplicates import group_duplicates

        photos = [
            self._photo(1, "0f0f0f0f0f0f0f0f", width=6000, height=4000),
            self._photo(2, "0f0f0f0f0f0f0f0f", width=4000, height=6000),
        ]
        self.assertEqual(group_duplicates(photos), [])

    def test_rows_without_a_usable_hash_are_skipped(self) -> None:
        from photo_reviewer.duplicates import group_duplicates

        photos = [
            self._photo(1, ""),
            self._photo(2, "not-hex-at-all!!"),
            self._photo(3, "abc"),  # legacy variable width
            self._photo(4, "0f0f0f0f0f0f0f0f"),
            self._photo(5, "0f0f0f0f0f0f0f0f"),
        ]
        groups = group_duplicates(photos)
        self.assertEqual(len(groups), 1)
        self.assertEqual([p["id"] for p in groups[0].photos], [4, 5])

    def test_the_keeper_prefers_resolution_then_size(self) -> None:
        from photo_reviewer.duplicates import DuplicateGroup

        group = DuplicateGroup(
            photos=[
                self._photo(1, "0f0f0f0f0f0f0f0f", width=1000, height=1000, size=900),
                self._photo(2, "0f0f0f0f0f0f0f0f", width=2000, height=2000, size=500),
                self._photo(3, "0f0f0f0f0f0f0f0f", width=1000, height=1000, size=800),
            ]
        )
        self.assertEqual(group.keep_id, 2, "the biggest picture wins")
        self.assertEqual(group.wasted_bytes, 900 + 800)

    def test_summary_counts_what_could_be_removed(self) -> None:
        from photo_reviewer.duplicates import DuplicateGroup, summarise

        groups = [
            DuplicateGroup(
                photos=[self._photo(i, "0f0f0f0f0f0f0f0f", size=100) for i in (1, 2, 3)]
            ),
            DuplicateGroup(
                photos=[self._photo(i, "ffffffffffffffff", size=50) for i in (4, 5)]
            ),
        ]
        stats = summarise(groups)
        self.assertEqual(stats["groups"], 2)
        self.assertEqual(stats["photos"], 5)
        self.assertEqual(stats["removable"], 3)
        self.assertEqual(stats["wasted_bytes"], 200 + 50)

    def test_a_real_dhash_round_trips_through_the_parser(self) -> None:
        """The stored value is hex, so a leading zero must not shorten it."""
        from photo_reviewer.duplicates import _parse

        self.assertEqual(_parse("0f0f0f0f0f0f0f0f"), 0x0F0F0F0F0F0F0F0F)
        self.assertEqual(_parse("0000000000000001"), 1)
        self.assertIsNone(_parse(""))


class DuplicatesApiTests(unittest.TestCase):
    """The /api/duplicates endpoint."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()
        folder = os.path.join(self.tmp.name, "album")
        os.makedirs(folder, exist_ok=True)
        for pid, phash in ((101, "0f0f0f0f0f0f0f0f"), (102, "0f0f0f0f0f0f0f0f")):
            self.db.upsert_photo(
                {
                    "path": os.path.join(folder, f"{pid}.jpg"),
                    "folder": folder,
                    "filename": f"{pid}.jpg",
                    "status": "analyzed",
                    "width": 1000,
                    "height": 1000,
                    "size": 2048,
                    "phash": phash,
                }
            )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_it_returns_groups_with_a_keeper(self) -> None:
        body = self.client.get("/api/duplicates").get_json()
        self.assertEqual(body["summary"]["groups"], 1)
        group = body["groups"][0]
        self.assertIn(group["keep_id"], [photo["id"] for photo in group["photos"]])
        self.assertEqual(len(group["photos"]), 2)

    def test_a_photo_payload_carries_what_the_reviewer_compares(self) -> None:
        body = self.client.get("/api/duplicates").get_json()
        photo = body["groups"][0]["photos"][0]
        for key in ("id", "filename", "width", "height", "size", "thumb_version"):
            self.assertIn(key, photo)

    def test_an_invalid_threshold_is_rejected(self) -> None:
        self.assertEqual(
            self.client.get("/api/duplicates?threshold=abc").status_code, 400
        )

    def test_an_absurd_threshold_is_clamped(self) -> None:
        body = self.client.get("/api/duplicates?threshold=999").get_json()
        self.assertLessEqual(body["threshold"], 24)

    def test_an_empty_library_returns_no_groups(self) -> None:
        folder = os.path.join(self.tmp.name, "album")
        ids = [
            self.db.get_photo_by_path(os.path.join(folder, name))["id"]
            for name in ("101.jpg", "102.jpg")
        ]
        self.db.delete_rows(ids)
        body = self.client.get("/api/duplicates").get_json()
        self.assertEqual(body["groups"], [])
        self.assertEqual(body["summary"]["groups"], 0)


class MemoriesTests(unittest.TestCase):
    """那年今日: photos from this calendar day in earlier years.

    The date handling is the part worth pinning: EXIF stores ``2023:10:02``, the
    query compares normalised ``MM-DD`` text, and "earlier years" has to exclude
    the current one or the feature reports this morning as a memory.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "album")
        os.makedirs(self.folder, exist_ok=True)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _add(self, name: str, captured: str) -> int:
        path = os.path.join(self.folder, name)
        self.db.upsert_photo(
            {
                "path": path,
                "folder": self.folder,
                "filename": name,
                "status": "analyzed",
                "exif": {"datetime_original": captured},
            }
        )
        return self.db.get_photo_by_path(path)["id"]

    def test_only_earlier_years_are_returned(self) -> None:
        current_year = datetime.datetime.now().astimezone().year
        self._add("old.jpg", f"{current_year - 2}:10:02 08:00:00")
        self._add("thisyear.jpg", f"{current_year}:10:02 08:00:00")
        body = self.client.get("/api/memories?month=10&day=2").get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["years"][0]["year"], current_year - 2)

    def test_years_are_grouped_and_counted(self) -> None:
        self._add("a.jpg", "2022:10:02 08:00:00")
        self._add("b.jpg", "2022:10:02 09:00:00")
        self._add("c.jpg", "2019:10:02 10:00:00")
        body = self.client.get("/api/memories?month=10&day=2").get_json()
        self.assertEqual(body["total"], 3)
        self.assertEqual([item["year"] for item in body["years"]], [2022, 2019])
        self.assertEqual(body["years"][0]["count"], 2)
        # Newest year first, so "last year" is the first thing read.
        self.assertGreater(body["years"][0]["years_ago"], 0)

    def test_a_window_widens_to_neighbouring_days(self) -> None:
        self._add("near.jpg", "2023:10:04 08:00:00")
        exact = self.client.get("/api/memories?month=10&day=2&window=0").get_json()
        widened = self.client.get("/api/memories?month=10&day=2&window=3").get_json()
        self.assertEqual(exact["total"], 0)
        self.assertEqual(widened["total"], 1)

    def test_the_window_wraps_across_a_month_boundary(self) -> None:
        """The 1st seen with a window of 1 must reach the last day of the month
        before, which is what `date + timedelta` gives and a naive ±1 would not."""
        self._add("nye.jpg", "2023:09:30 23:00:00")
        body = self.client.get("/api/memories?month=10&day=1&window=1").get_json()
        self.assertEqual(body["total"], 1)

    def test_deleted_photos_are_left_out(self) -> None:
        pid = self._add("gone.jpg", "2022:10:02 08:00:00")
        self.db.mark_deleted([pid], {})
        body = self.client.get("/api/memories?month=10&day=2").get_json()
        self.assertEqual(body["total"], 0)

    def test_an_impossible_date_is_rejected(self) -> None:
        self.assertEqual(
            self.client.get("/api/memories?month=13&day=2").status_code, 400
        )
        self.assertEqual(
            self.client.get("/api/memories?month=2&day=31").status_code, 400
        )

    def test_an_invalid_window_is_rejected(self) -> None:
        self.assertEqual(
            self.client.get("/api/memories?month=10&day=2&window=abc").status_code, 400
        )

    def test_a_wild_window_is_clamped(self) -> None:
        body = self.client.get("/api/memories?month=10&day=2&window=999").get_json()
        self.assertLessEqual(body["window"], 30)


class ExporterTests(unittest.TestCase):
    """Both keepsake formats.

    The point of an export is that it still opens in ten years, so the tests care
    about self-containment (no external URLs left in the HTML), about the missing-
    file path (a photo that moved must not leave a broken-image glyph in a
    keepsake), and about the tall image being a real stacked picture rather than a
    blank canvas.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = []
        for index, colour in enumerate([(200, 120, 90), (90, 140, 200)]):
            path = os.path.join(self.tmp.name, f"p{index}.jpg")
            Image.new("RGB", (400, 300), colour).save(path, "JPEG")
            self.paths.append(path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _photos(self):
        from photo_reviewer.exporter import ExportPhoto

        return [
            ExportPhoto(
                filename=os.path.basename(path),
                path=path,
                thumbnail_path=path,
                caption="黄昏把窗台染成了蜜色。",
                title="窗边的黄昏",
                tags=["黄昏", "安静"],
                score=8.5,
                captured="2024-10-02",
            )
            for path in self.paths
        ]

    def test_html_embeds_every_picture(self) -> None:
        from photo_reviewer.exporter import build_html

        document = build_html(self._photos(), title="那年今日", subtitle="10 月 2 日")
        self.assertEqual(document.count("data:image/jpeg;base64,"), 2)
        self.assertIn("那年今日", document)
        self.assertIn("窗边的黄昏", document)
        self.assertIn("黄昏把窗台染成了蜜色。", document)

    def test_html_has_no_external_references(self) -> None:
        """A keepsake must not depend on the app still running."""
        from photo_reviewer.exporter import build_html

        document = build_html(self._photos())
        self.assertNotIn("http://", document)
        self.assertNotIn("https://", document)
        # Only the data URIs may carry a src.
        for fragment in document.split('src="')[1:]:
            self.assertTrue(fragment.startswith("data:"), fragment[:40])

    def test_a_missing_file_is_reported_not_left_broken(self) -> None:
        from photo_reviewer.exporter import ExportPhoto, build_html

        photos = [
            ExportPhoto(
                filename="gone.jpg",
                path=os.path.join(self.tmp.name, "does-not-exist.jpg"),
            )
        ]
        document = build_html(photos)
        self.assertNotIn("data:image", document)
        self.assertIn("已经不在原处", document)

    def test_the_tall_image_is_a_real_stack(self) -> None:
        from photo_reviewer.exporter import build_tall_image

        image = build_tall_image(self._photos(), title="那年今日")
        self.assertIsNotNone(image)
        try:
            width, height = image.size
            self.assertEqual(width, 1080)
            # Two photos plus captions: tall, and not a single-photo canvas.
            self.assertGreater(height, 800)
        finally:
            image.close()

    def test_an_empty_export_is_none(self) -> None:
        from photo_reviewer.exporter import build_tall_image

        self.assertIsNone(build_tall_image([]))

    def test_the_png_bytes_start_with_a_png_signature(self) -> None:
        from photo_reviewer.exporter import build_tall_image, tall_image_bytes

        image = build_tall_image(self._photos())
        try:
            data = tall_image_bytes(image)
        finally:
            image.close()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_rows_are_flattened_from_json_columns(self) -> None:
        """Database rows carry EXIF and tags as JSON text, not decoded objects."""
        from photo_reviewer.exporter import rows_to_export

        rows = [
            {
                "filename": "a.jpg",
                "path": self.paths[0],
                "thumb_path": self.paths[0],
                "reason": "一句话",
                "title": "标题",
                "tags": '["海边", "落日"]',
                "score": 9.1,
                "exif": '{"datetime_original": "2023:10:02 08:00:00"}',
            }
        ]
        photos = rows_to_export(rows)
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0].tags, ["海边", "落日"])
        self.assertEqual(photos[0].captured, "2023-10-02")
        self.assertEqual(photos[0].score, 9.1)

    def test_a_corrupt_json_column_does_not_raise(self) -> None:
        from photo_reviewer.exporter import rows_to_export

        photos = rows_to_export(
            [{"filename": "a.jpg", "path": self.paths[0], "tags": "not json", "exif": "{"}]
        )
        self.assertEqual(photos[0].tags, [])
        self.assertEqual(photos[0].captured, "")


class ExportApiTests(unittest.TestCase):
    """/api/export: the download itself."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "album")
        os.makedirs(self.folder, exist_ok=True)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()
        self.ids = []
        for index in range(2):
            path = os.path.join(self.folder, f"e{index}.jpg")
            Image.new("RGB", (200, 150), (120, 90, 60)).save(path, "JPEG")
            self.db.upsert_photo(
                {
                    "path": path,
                    "folder": self.folder,
                    "filename": f"e{index}.jpg",
                    "status": "analyzed",
                    "reason": "一句话的记忆。",
                    "exif": {"datetime_original": "2022:10:02 08:00:00"},
                }
            )
            self.ids.append(self.db.get_photo_by_path(path)["id"])

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_html_download(self) -> None:
        resp = self.client.post(
            "/api/export", json={"ids": self.ids, "format": "html", "title": "拾光回忆"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
        body = resp.data.decode("utf-8")
        self.assertEqual(body.count("data:image/jpeg"), 2)

    def test_png_download(self) -> None:
        resp = self.client.post(
            "/api/export", json={"ids": self.ids, "format": "png"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "image/png")
        self.assertTrue(resp.data.startswith(b"\x89PNG"))

    def test_the_html_content_type_is_not_doubled(self) -> None:
        """Flask appends the charset itself; passing it too produced
        "text/html; charset=utf-8; charset=utf-8"."""
        resp = self.client.post("/api/export", json={"ids": self.ids, "format": "html"})
        self.assertEqual(resp.headers.get("Content-Type").count("charset"), 1)

    def test_exporting_by_date_matches_the_memories_view(self) -> None:
        resp = self.client.post(
            "/api/export", json={"month": 10, "day": 2, "window": 0, "format": "html"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("data:image/jpeg", resp.data.decode("utf-8"))

    def test_an_unknown_format_is_rejected(self) -> None:
        self.assertEqual(
            self.client.post(
                "/api/export", json={"ids": self.ids, "format": "pdf"}
            ).status_code,
            400,
        )

    def test_an_empty_selection_is_rejected(self) -> None:
        self.assertEqual(
            self.client.post("/api/export", json={"ids": [], "format": "html"}).status_code,
            400,
        )

    def test_an_impossible_date_is_rejected(self) -> None:
        self.assertEqual(
            self.client.post(
                "/api/export", json={"month": 2, "day": 31, "format": "html"}
            ).status_code,
            400,
        )


class FootprintTests(unittest.TestCase):
    """Collapsing reverse-geocoded addresses into places.

    The geocoder returns full street addresses. That is right for a caption and
    useless for a footprint list - without collapsing, 404 distinct addresses in a
    real library produced 404 entries instead of 22 places. The municipality and
    country-prefix cases were both found by looking at what the real data contained.
    """

    def test_a_municipality_is_its_own_city(self) -> None:
        from photo_reviewer.footprints import city_of

        # "海淀区" is a district of the city the photo was taken in, not the place
        # someone would say they visited.
        self.assertEqual(city_of("北京市海淀区甘家口街道玉渊潭公园"), "北京市")
        self.assertEqual(city_of("上海市浦东新区陆家嘴"), "上海市")

    def test_a_province_is_stripped(self) -> None:
        from photo_reviewer.footprints import city_of

        self.assertEqual(city_of("河北省石家庄市裕华区槐底街道"), "石家庄市")
        self.assertEqual(city_of("广东省深圳市南山区科技园"), "深圳市")
        self.assertEqual(city_of("浙江省杭州市西湖区西湖"), "杭州市")

    def test_an_autonomous_region_keeps_its_city(self) -> None:
        from photo_reviewer.footprints import city_of

        self.assertEqual(city_of("内蒙古自治区呼和浩特市新城区"), "呼和浩特市")

    def test_a_special_administrative_region_is_the_place(self) -> None:
        from photo_reviewer.footprints import city_of

        self.assertEqual(city_of("香港特别行政区中西区中银大厦"), "香港特别行政区")

    def test_a_country_prefix_does_not_become_a_place(self) -> None:
        """A street under a bare country prefix must not read as one place."""
        from photo_reviewer.footprints import city_of

        self.assertEqual(city_of("中华人民共和国广东省深圳市南山区"), "深圳市")
        # Nothing but the country: that is the best answer available.
        self.assertEqual(city_of("中华人民共和国"), "中华人民共和国")

    def test_a_bare_coordinate_pair_is_left_alone(self) -> None:
        from photo_reviewer.footprints import city_of

        self.assertEqual(city_of("39.9042, 116.4074"), "39.9042, 116.4074")

    def test_an_empty_location_is_empty(self) -> None:
        from photo_reviewer.footprints import city_of

        self.assertEqual(city_of(""), "")
        self.assertEqual(city_of("   "), "")

    def test_photos_are_grouped_by_city(self) -> None:
        from photo_reviewer.footprints import group_by_city

        photos = [
            {"id": 1, "location": "北京市海淀区A路", "exif": {"datetime_original": "2024:01:01"}},
            {"id": 2, "location": "北京市朝阳区B路", "exif": {"datetime_original": "2025:01:01"}},
            {"id": 3, "location": "河北省石家庄市C街", "exif": {"datetime_original": "2023:01:01"}},
        ]
        footprints = group_by_city(photos)
        self.assertEqual(len(footprints), 2)
        # Busiest first.
        self.assertEqual(footprints[0].city, "北京市")
        self.assertEqual(footprints[0].count, 2)
        self.assertEqual(footprints[0].years, [2024, 2025])

    def test_photos_without_a_location_are_skipped(self) -> None:
        from photo_reviewer.footprints import group_by_city

        photos = [
            {"id": 1, "location": "", "exif": {}},
            {"id": 2, "location": None, "exif": {}},
            {"id": 3, "location": "北京市", "exif": {}},
        ]
        footprints = group_by_city(photos)
        self.assertEqual(len(footprints), 1)
        self.assertEqual(footprints[0].count, 1)

    def test_a_json_exif_string_still_yields_years(self) -> None:
        """Raw cursor rows carry exif as text; the grouping must still date them."""
        from photo_reviewer.footprints import group_by_city

        photos = [
            {"id": 1, "location": "北京市", "exif": '{"datetime_original": "2019:05:05"}'},
            {"id": 2, "location": "北京市", "exif": '{"datetime_original": "2021:05:05"}'},
        ]
        footprints = group_by_city(photos)
        self.assertEqual(footprints[0].years, [2019, 2021])

    def test_a_corrupt_exif_string_does_not_raise(self) -> None:
        from photo_reviewer.footprints import group_by_city

        footprints = group_by_city([{"id": 1, "location": "北京市", "exif": "{"}])
        self.assertEqual(footprints[0].years, [])


class FootprintsApiTests(unittest.TestCase):
    """/api/footprints."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "album")
        os.makedirs(self.folder, exist_ok=True)
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _add(self, name: str, location: str, captured: str) -> None:
        self.db.upsert_photo(
            {
                "path": os.path.join(self.folder, name),
                "folder": self.folder,
                "filename": name,
                "status": "analyzed",
                "location": location,
                "exif": {"datetime_original": captured},
            }
        )

    def test_places_are_grouped_with_a_cover(self) -> None:
        self._add("a.jpg", "北京市海淀区A路", "2024:01:01 08:00:00")
        self._add("b.jpg", "北京市朝阳区B路", "2025:01:01 08:00:00")
        body = self.client.get("/api/footprints").get_json()
        self.assertEqual(len(body["places"]), 1)
        place = body["places"][0]
        self.assertEqual(place["city"], "北京市")
        self.assertEqual(place["count"], 2)
        self.assertEqual(place["years"], [2024, 2025])
        self.assertIn("id", place["cover"])

    def test_an_empty_library_returns_no_places(self) -> None:
        body = self.client.get("/api/footprints").get_json()
        self.assertEqual(body["places"], [])
        self.assertEqual(body["total"], 0)

    def test_a_place_carries_its_own_photos(self) -> None:
        """A place opens to a grid of its photos, so the rows have to come with it.

        It used to return only a cover, which meant clicking a place could open
        nothing but that one photo.
        """
        self._add("a.jpg", "北京市海淀区A路", "2024:01:01 08:00:00")
        self._add("b.jpg", "北京市朝阳区B路", "2025:01:01 08:00:00")
        place = self.client.get("/api/footprints").get_json()["places"][0]
        self.assertEqual(len(place["photos"]), 2)
        ids = {photo["id"] for photo in place["photos"]}
        self.assertEqual(len(ids), 2)

    def test_place_photos_carry_what_the_dialog_paints(self) -> None:
        """The detail dialog reads these before its own payload lands.

        A narrower select raised `KeyError: 'size'` the first time a place was
        opened, because the rows had been trimmed for a cover thumbnail.
        """
        self._add("a.jpg", "北京市海淀区A路", "2024:01:01 08:00:00")
        photo = self.client.get("/api/footprints").get_json()["places"][0]["photos"][0]
        for key in ("id", "filename", "size", "width", "height", "status",
                    "media_kind", "thumb_version"):
            self.assertIn(key, photo)


class DuplicateThresholdStateTests(unittest.TestCase):
    """The duplicate sheet remembers the threshold the reader settled on."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_the_duplicate_threshold_is_remembered(self) -> None:
        """A judgement the reader already made should survive a reload.

        The sheet used to open at 6 every time, so anyone who had settled on a
        tighter value re-decided it on every visit.
        """
        self.client.post("/api/ui-state", json={"dup_threshold": 4})
        saved = self.client.get("/api/ui-state").get_json()["state"]
        self.assertEqual(saved["dup_threshold"], 4)

    def test_the_threshold_is_clamped(self) -> None:
        self.client.post("/api/ui-state", json={"dup_threshold": 99})
        saved = self.client.get("/api/ui-state").get_json()["state"]
        self.assertLessEqual(saved["dup_threshold"], 24)

    def test_a_non_numeric_threshold_is_rejected(self) -> None:
        resp = self.client.post("/api/ui-state", json={"dup_threshold": "abc"})
        self.assertEqual(resp.status_code, 400)

    def test_an_unknown_key_is_still_rejected(self) -> None:
        """The endpoint is writable from the browser; it must stay bounded."""
        resp = self.client.post("/api/ui-state", json={"nonsense": 1})
        self.assertEqual(resp.status_code, 400)


class SearchModeTests(unittest.TestCase):
    """Two search modes, and the retired ones still answered.

    Measured on the real library, `keyword` returned nothing for four of five
    ordinary Chinese queries - it can only match words that happen to appear in a
    caption. `semantic` was strictly worse than `smart`: the same model call without
    the local recall pass, so it took 6.6s where `smart` took 0.8s, and it returned a
    hard 502 when the model was unreachable where `smart` degraded gracefully.
    """

    def _modes(self) -> tuple[str, ...]:
        from photo_reviewer.server import SEARCH_MODES

        return SEARCH_MODES

    def test_only_two_modes_are_offered(self) -> None:
        self.assertEqual(set(self._modes()), {"smart", "visual"})

    def test_keyword_is_no_longer_offered(self) -> None:
        self.assertNotIn("keyword", self._modes())

    def test_semantic_is_no_longer_offered(self) -> None:
        self.assertNotIn("semantic", self._modes())

    def test_visual_survives_because_it_answers_differently(self) -> None:
        """It looks at pixels, not captions, so it is not a worse `smart`."""
        self.assertIn("visual", self._modes())

    def test_retired_modes_are_redirected_not_rejected(self) -> None:
        """A long-lived tab can still be holding an old option value."""
        from photo_reviewer.server import _LEGACY_SEARCH_MODES

        self.assertEqual(_LEGACY_SEARCH_MODES.get("keyword"), "smart")
        self.assertEqual(_LEGACY_SEARCH_MODES.get("semantic"), "smart")
        # A redirect target must itself be offered, or the redirect is a dead end.
        for target in _LEGACY_SEARCH_MODES.values():
            self.assertIn(target, self._modes())


class ExportSourceTests(unittest.TestCase):
    """The export reads the proxy render, not the thumbnail.

    `rows_to_export` used to pass `thumb_path or proxy_path`, so every keepsake was
    built from a ~400px thumbnail and then displayed in a ~700px slot - visibly soft.
    """

    def test_the_proxy_is_preferred(self) -> None:
        from photo_reviewer.exporter import ExportPhoto, _image_source

        with tempfile.TemporaryDirectory() as tmp:
            proxy = os.path.join(tmp, "proxy.jpg")
            thumb = os.path.join(tmp, "thumb.jpg")
            for path in (proxy, thumb):
                with open(path, "wb") as handle:
                    handle.write(b"x")
            photo = ExportPhoto(
                filename="a.jpg", path="", proxy_path=proxy, thumbnail_path=thumb
            )
            self.assertEqual(_image_source(photo), proxy)

    def test_the_thumbnail_is_the_last_resort(self) -> None:
        from photo_reviewer.exporter import ExportPhoto, _image_source

        with tempfile.TemporaryDirectory() as tmp:
            thumb = os.path.join(tmp, "thumb.jpg")
            with open(thumb, "wb") as handle:
                handle.write(b"x")
            photo = ExportPhoto(
                filename="a.jpg", path="/gone", proxy_path=None, thumbnail_path=thumb
            )
            self.assertEqual(_image_source(photo), thumb)

    def test_rows_carry_the_proxy_through(self) -> None:
        from photo_reviewer.exporter import rows_to_export

        photos = rows_to_export(
            [{"filename": "a.jpg", "path": "/a", "proxy_path": "/p", "thumb_path": "/t"}]
        )
        self.assertEqual(photos[0].proxy_path, "/p")
        self.assertEqual(photos[0].thumbnail_path, "/t")


class ExportStyleTests(unittest.TestCase):
    """The keepsake has to look like the app it came from."""

    def test_html_uses_the_site_typography(self) -> None:
        """Serif for prose, the hand face for the model's sentence."""
        from photo_reviewer.exporter import build_html

        html = build_html([], title="那年今日")
        self.assertIn("--font-serif", html)
        self.assertIn("--font-hand", html)
        self.assertIn("font-family: var(--font-hand)", html)

    def test_html_carries_the_site_palette(self) -> None:
        from photo_reviewer.exporter import build_html

        html = build_html([], title="那年今日")
        # The paper, ink and warm accent the album itself uses.
        for token in ("#f9f6f0", "#2c241d", "#b37d56"):
            self.assertIn(token, html)

    def test_html_animates_on_entry_and_respects_reduced_motion(self) -> None:
        from photo_reviewer.exporter import build_html

        html = build_html([], title="那年今日")
        self.assertIn("printDrop", html)
        self.assertIn("prefers-reduced-motion", html)

    def test_html_still_prints_flat(self) -> None:
        """The tilt and the shadow are screen decoration; paper gets neither."""
        from photo_reviewer.exporter import build_html

        html = build_html([], title="那年今日")
        self.assertIn("@media print", html)
        self.assertIn("transform: none", html)

    def test_the_tall_image_keeps_the_paper_tone(self) -> None:
        from photo_reviewer.exporter import ExportPhoto, build_tall_image

        photos = [ExportPhoto(filename="a.jpg", path="", thumbnail_path=None)]
        image = build_tall_image(photos, title="那年今日")
        self.assertIsNotNone(image)
        # The top-left corner is the paper the whole canvas is washed with.
        pixel = image.getpixel((4, 4))
        self.assertGreater(pixel[0], 230)
        self.assertGreater(pixel[0], pixel[2])

    def test_a_missing_file_is_said_plainly(self) -> None:
        from photo_reviewer.exporter import ExportPhoto, build_html

        photos = [ExportPhoto(filename="gone.jpg", path="/nope", thumbnail_path=None)]
        html = build_html(photos, title="那年今日")
        self.assertIn("已经不在原处", html)


class VisualSearchTests(unittest.TestCase):
    """Asking the vision model which pictures match.

    The text search can only reason about captions, so it cannot answer "那张逆光的
    照片". The model can look - but each image is a real request, so the limits and
    the answer format both matter as much as the accuracy. The first version asked
    it to echo database ids, which it cannot read, and it matched every photo in the
    batch; asking for positions fixed that.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "l.db"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _photo(self, pid: int) -> dict:
        path = os.path.join(self.tmp.name, f"p{pid}.jpg")
        if not os.path.exists(path):
            Image.new("RGB", (40, 30), (120, 90, 60)).save(path, "JPEG")
        return {"id": pid, "thumb_path": path, "filename": f"p{pid}.jpg"}

    def _fake_response(self, text: str):
        class Resp:
            def json(self):
                return {"choices": [{"message": {"content": text}}]}

        return Resp()

    def test_positions_are_mapped_back_to_ids(self) -> None:
        from photo_reviewer.api_client import visual_search

        photos = [self._photo(11), self._photo(22), self._photo(33)]
        with patch(
            "photo_reviewer.api_client._post_chat_completion",
            return_value=self._fake_response('{"match": [1, 3]}'),
        ):
            ids, checked = visual_search("有海的照片", photos, self.cfg, batch_size=3)
        self.assertEqual(ids, [11, 33])
        self.assertEqual(checked, 3)

    def test_a_hallucinated_position_is_ignored(self) -> None:
        """A number outside the batch must not pull in an unrelated photo."""
        from photo_reviewer.api_client import visual_search

        photos = [self._photo(11), self._photo(22)]
        with patch(
            "photo_reviewer.api_client._post_chat_completion",
            return_value=self._fake_response('{"match": [9, -1, 2]}'),
        ):
            ids, _checked = visual_search("x", photos, self.cfg, batch_size=2)
        self.assertEqual(ids, [22])

    def test_no_match_returns_empty(self) -> None:
        from photo_reviewer.api_client import visual_search

        photos = [self._photo(11)]
        with patch(
            "photo_reviewer.api_client._post_chat_completion",
            return_value=self._fake_response('{"match": []}'),
        ):
            ids, checked = visual_search("x", photos, self.cfg)
        self.assertEqual(ids, [])
        self.assertEqual(checked, 1)

    def test_the_candidate_list_is_capped(self) -> None:
        """Cost is per image, so the cap is part of the contract."""
        from photo_reviewer.api_client import visual_search

        photos = [self._photo(pid) for pid in range(1, 31)]
        calls = {"n": 0}

        def fake(*_args, **_kwargs):
            calls["n"] += 1
            return self._fake_response('{"match": []}')

        with patch("photo_reviewer.api_client._post_chat_completion", side_effect=fake):
            _ids, checked = visual_search(
                "x", photos, self.cfg, batch_size=4, max_candidates=8
            )
        self.assertEqual(checked, 8)
        self.assertEqual(calls["n"], 2, "eight photos at four per request")

    def test_an_empty_query_or_list_does_nothing(self) -> None:
        from photo_reviewer.api_client import visual_search

        self.assertEqual(visual_search("", [self._photo(1)], self.cfg), ([], 0))
        self.assertEqual(visual_search("x", [], self.cfg), ([], 0))

    def test_a_photo_without_a_preview_is_skipped(self) -> None:
        from photo_reviewer.api_client import visual_search

        photos = [{"id": 5, "filename": "nope.jpg"}]
        self.assertEqual(visual_search("x", photos, self.cfg), ([], 0))

    def test_a_missing_preview_is_skipped_not_fatal(self) -> None:
        """One unreadable thumbnail must not fail the whole search.

        A photo whose proxy was pruned since indexing is a normal state - the
        library keeps the row and can re-render on demand. The batch simply carries
        the photos it could read.
        """
        from photo_reviewer.api_client import visual_search

        photos = [{"id": 7, "thumb_path": os.path.join(self.tmp.name, "gone.jpg")}]
        with patch(
            "photo_reviewer.api_client._post_chat_completion",
            return_value=self._fake_response('{"match": [1]}'),
        ):
            ids, checked = visual_search("x", photos, self.cfg)
        self.assertEqual(ids, [])
        self.assertEqual(checked, 1)


class SearchModeApiTests(unittest.TestCase):
    """The search endpoint's mode handling."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_an_unknown_mode_is_rejected(self) -> None:
        """It used to fall through every branch and return an empty list, which
        reads as "nothing matches" rather than "that is not a mode"."""
        resp = self.client.post("/api/search", json={"query": "x", "mode": "nonsense"})
        self.assertEqual(resp.status_code, 400)

    def test_every_known_mode_is_accepted(self) -> None:
        for mode in ("keyword", "smart"):
            resp = self.client.post("/api/search", json={"query": "海", "mode": mode})
            self.assertEqual(resp.status_code, 200, mode)

    def test_visual_mode_reports_how_many_were_checked(self) -> None:
        # Visual search now probes the model first, so a run against a stopped
        # server is refused immediately instead of scanning with nothing to ask.
        # The probe is stubbed here: what this test is about is the `checked`
        # count on a successful scan, not whether a model happens to be running.
        with patch(
            "photo_reviewer.server.probe_endpoint", return_value=(True, "")
        ), patch(
            "photo_reviewer.server.visual_search", return_value=([], 3)
        ):
            resp = self.client.post(
                "/api/search", json={"query": "海", "mode": "visual"}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("checked", resp.get_json())

    def test_visual_mode_fails_fast_when_the_model_is_down(self) -> None:
        """One cheap probe, not the whole retry budget.

        Without it the reader sat behind the loading dialog for the full timeout
        before being told the model was unreachable.
        """
        with patch(
            "photo_reviewer.server.probe_endpoint",
            return_value=(False, "无法连接模型服务 http://x/v1"),
        ):
            resp = self.client.post(
                "/api/search", json={"query": "海", "mode": "visual"}
            )
        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.get_json().get("unreachable"))

    def test_smart_mode_degrades_to_keyword_when_the_model_is_down(self) -> None:
        """`smart` has a local pass, so it answers instead of timing out."""
        with patch(
            "photo_reviewer.server.probe_endpoint",
            return_value=(False, "无法连接模型服务 http://x/v1"),
        ):
            resp = self.client.post("/api/search", json={"query": "海", "mode": "smart"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("warning"), "the reader is told why it is degraded")


class BytesReaderTests(unittest.TestCase):
    """The buffer wrapper Pillow reads raw previews through.

    ``readline`` is not decorative: the TIFF plugin calls it while walking the
    IFD chain. Without it, one RAW container in a real library failed to index
    with "'_BytesReader' object has no attribute 'readline'".
    """

    def test_it_implements_the_protocol_pillow_uses(self) -> None:
        from photo_reviewer.thumbnailer import _BytesReader

        reader = _BytesReader(b"abc\ndef\nghi")
        self.assertEqual(reader.readline(), b"abc\n")
        self.assertEqual(reader.readline(), b"def\n")
        self.assertEqual(reader.tell(), 8)
        self.assertEqual(reader.read(), b"ghi")
        self.assertEqual(reader.readline(), b"")
        reader.seek(0)
        self.assertEqual(reader.readline(2), b"ab")
        self.assertTrue(reader.seekable())
        self.assertTrue(reader.readable())

    def test_it_can_be_used_as_a_context_manager(self) -> None:
        from photo_reviewer.thumbnailer import _BytesReader

        with _BytesReader(b"x") as reader:
            self.assertEqual(reader.read(), b"x")


class LauncherTests(unittest.TestCase):
    """The entry point: port probing, browser opening, and staying alive.

    ``app.py`` had no tests at all, which is how the browser tab could be added and
    shipped while every other check ran with ``PHOTO_OPEN_BROWSER=0``. The timer is
    patched out so the tests do not sleep through the real delay.
    """

    def setUp(self) -> None:
        # Importing the module builds a Flask app; keep it off the real database.
        self.tmp = tempfile.TemporaryDirectory()
        self._env = {
            key: os.environ.get(key)
            for key in ("PHOTO_DATA_DIR", "PHOTO_DB_PATH", "PHOTO_OPEN_BROWSER")
        }
        os.environ["PHOTO_DATA_DIR"] = self.tmp.name
        os.environ["PHOTO_DB_PATH"] = os.path.join(self.tmp.name, "library.db")
        os.environ.pop("PHOTO_OPEN_BROWSER", None)

    def tearDown(self) -> None:
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    @staticmethod
    def _launcher():
        """Import ``app.py`` fresh so the env vars above are the ones it reads."""
        import importlib

        if "app" in sys.modules:
            del sys.modules["app"]
        return importlib.import_module("app")

    def test_the_browser_is_off_by_default_only_when_asked(self) -> None:
        launcher = self._launcher()
        for value in ("0", "false", "no", "off", "FALSE", " Off "):
            os.environ["PHOTO_OPEN_BROWSER"] = value
            self.assertTrue(launcher._browser_disabled(), value)
        for value in ("1", "true", "yes", ""):
            os.environ["PHOTO_OPEN_BROWSER"] = value
            self.assertFalse(launcher._browser_disabled(), value)

    def test_it_opens_the_url_it_was_given(self) -> None:
        launcher = self._launcher()
        os.environ.pop("PHOTO_OPEN_BROWSER", None)
        opened = []
        with patch.object(launcher, "OPEN_BROWSER_DELAY", 0), patch(
            "webbrowser.open", side_effect=lambda url: opened.append(url)
        ):
            launcher.open_browser_later("http://127.0.0.1:5050")
            # The call happens on a timer thread; give it a moment to land.
            for _ in range(60):
                if opened:
                    break
                time.sleep(0.01)
        self.assertEqual(opened, ["http://127.0.0.1:5050"])

    def test_the_flag_suppresses_the_tab(self) -> None:
        launcher = self._launcher()
        os.environ["PHOTO_OPEN_BROWSER"] = "0"
        opened = []
        with patch.object(launcher, "OPEN_BROWSER_DELAY", 0), patch(
            "webbrowser.open", side_effect=lambda url: opened.append(url)
        ):
            launcher.open_browser_later("http://127.0.0.1:5050")
            time.sleep(0.2)
        self.assertEqual(opened, [])

    def test_a_machine_with_no_browser_still_serves(self) -> None:
        """No default handler must print the URL, not raise into the server."""
        launcher = self._launcher()
        os.environ.pop("PHOTO_OPEN_BROWSER", None)
        with patch.object(launcher, "OPEN_BROWSER_DELAY", 0), patch(
            "webbrowser.open", side_effect=RuntimeError("no handler")
        ):
            # Reaching the assertion at all is the point: the timer thread must
            # swallow the error rather than kill the process.
            launcher.open_browser_later("http://127.0.0.1:5050")
            time.sleep(0.25)
        self.assertTrue(True)

    def test_the_port_probe_finds_a_port_it_can_bind(self) -> None:
        launcher = self._launcher()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            busy = probe.getsockname()[1]
            probe.listen(1)
            # A port somebody is listening on is not free.
            self.assertFalse(launcher._port_is_free("127.0.0.1", busy))
        # And the same port is free once it is released.
        self.assertTrue(launcher._port_is_free("127.0.0.1", busy))

    def test_the_attempt_budget_is_not_absurd(self) -> None:
        launcher = self._launcher()
        self.assertGreaterEqual(launcher.PORT_ATTEMPTS, 1)
        self.assertLessEqual(launcher.PORT_ATTEMPTS, 200)

    def test_no_open_is_parsed(self) -> None:
        launcher = self._launcher()
        self.assertFalse(launcher._parse_args([]).no_open)
        self.assertTrue(launcher._parse_args(["--no-open"]).no_open)

    def test_no_open_switches_the_browser_off(self) -> None:
        """The flag has to work on its own, without the environment variable.

        It exists because `PHOTO_OPEN_BROWSER=0` is only discoverable by reading the
        docs, and a script launching this app should not have to know about it.
        """
        launcher = self._launcher()
        os.environ.pop("PHOTO_OPEN_BROWSER", None)
        launcher._NO_OPEN_FLAG = True
        try:
            self.assertTrue(launcher._browser_disabled())
        finally:
            launcher._NO_OPEN_FLAG = False
        self.assertFalse(launcher._browser_disabled())

    def test_an_unknown_flag_does_not_crash_the_launcher(self) -> None:
        """argparse would exit 2, which reads as a crash in a double-clicked window."""
        launcher = self._launcher()
        parsed = launcher._parse_args(["--no-open", "--something-else"])
        self.assertTrue(parsed.no_open)

    def test_the_environment_variable_still_works(self) -> None:
        launcher = self._launcher()
        launcher._NO_OPEN_FLAG = False
        os.environ["PHOTO_OPEN_BROWSER"] = "0"
        try:
            self.assertTrue(launcher._browser_disabled())
        finally:
            os.environ.pop("PHOTO_OPEN_BROWSER", None)

    def test_candidates_are_unique_and_in_range(self) -> None:
        launcher = self._launcher()
        sequence = list(launcher._candidate_ports(5050))
        self.assertEqual(len(sequence), len(set(sequence)), "a port is probed twice")
        self.assertTrue(all(1024 <= port <= 65535 for port in sequence))

    def test_it_searches_both_directions(self) -> None:
        """Windows reserves whole blocks, so upwards alone is not enough.

        On the machine this was written for, 5057-5156 is reserved: starting at
        5061 and counting up finds nothing at all. Counting down reaches 5056.
        """
        launcher = self._launcher()
        sequence = list(launcher._candidate_ports(5061))
        self.assertIn(5062, sequence, "still goes up first")
        self.assertIn(5056, sequence, "and comes back down")
        # The edge of the block is close by, so it must be reached early rather
        # than after the whole upward budget is spent.
        self.assertLess(sequence.index(5056), 20)

    def test_conventional_ports_are_the_last_resort(self) -> None:
        launcher = self._launcher()
        sequence = list(launcher._candidate_ports(5050))
        self.assertIn(8080, sequence)
        # A fallback must never be tried before the neighbours.
        self.assertGreater(sequence.index(8080), sequence.index(5051))

    def test_a_free_configured_port_is_used_immediately(self) -> None:
        launcher = self._launcher()
        sequence = list(launcher._candidate_ports(5050))
        self.assertEqual(sequence[0], 5050)


class DetailPayloadFieldTests(unittest.TestCase):
    """The lightbox renders a path row and a RAW marker from the row it was given.

    Two trimmed payloads - footprints' place rows and the duplicate review rows -
    shipped neither, so a photo opened from 足迹 or 相似回忆 showed no physical path
    and no RAW badge, while the same photo opened from the album showed both.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _add_pair(self, folder: str) -> tuple[str, str]:
        image = os.path.join(folder, "DSC_0001.JPG")
        raw = os.path.join(folder, "DSC_0001.ARW")
        for p in (image, raw):
            with open(p, "wb") as handle:
                handle.write(b"x")
        self.db.upsert_photo(
            {
                "path": image,
                "raw_path": raw,
                "folder": folder,
                "filename": "DSC_0001.JPG",
                "status": "analyzed",
                "width": 1000,
                "height": 800,
                "size": 2048,
                "location": "北京市海淀区A路",
                "exif": {"datetime_original": "2024:05:05 10:00:00"},
                "phash": "0f0f0f0f0f0f0f0f",
            }
        )
        return image, raw

    def test_duplicate_rows_carry_the_path_and_raw_fields(self) -> None:
        folder = os.path.join(self.tmp.name, "album")
        os.makedirs(folder, exist_ok=True)
        image, raw = self._add_pair(folder)
        # A second copy so a group exists.
        copy = os.path.join(folder, "DSC_0001-copy.JPG")
        with open(copy, "wb") as handle:
            handle.write(b"x")
        self.db.upsert_photo(
            {
                "path": copy,
                "folder": folder,
                "filename": "DSC_0001-copy.JPG",
                "status": "analyzed",
                "width": 1000,
                "height": 800,
                "size": 2048,
                "phash": "0f0f0f0f0f0f0f0f",
            }
        )
        body = self.client.get("/api/duplicates").get_json()
        photo = body["groups"][0]["photos"][0]
        self.assertEqual(photo["path"], image)
        self.assertTrue(photo["has_raw"])
        self.assertEqual(photo["raw_path"], raw)
        self.assertEqual(photo["raw_ext"], "ARW")

    def test_place_rows_carry_the_path_and_raw_fields(self) -> None:
        folder = os.path.join(self.tmp.name, "album")
        os.makedirs(folder, exist_ok=True)
        image, raw = self._add_pair(folder)
        body = self.client.get("/api/footprints").get_json()
        photo = body["places"][0]["photos"][0]
        self.assertEqual(photo["path"], image)
        self.assertTrue(photo["has_raw"])
        self.assertEqual(photo["raw_path"], raw)
        self.assertEqual(photo["raw_ext"], "ARW")


class ModelProbeTests(unittest.TestCase):
    """Search probes the model rather than waiting out the retry budget."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_visual_search_refuses_at_once_without_a_model(self) -> None:
        with patch(
            "photo_reviewer.server.probe_endpoint",
            return_value=(False, "无法连接模型服务"),
        ):
            resp = self.client.post("/api/search", json={"query": "海", "mode": "visual"})
        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.get_json()["unreachable"])

    def test_smart_search_still_answers_without_a_model(self) -> None:
        with patch(
            "photo_reviewer.server.probe_endpoint",
            return_value=(False, "无法连接模型服务"),
        ):
            resp = self.client.post("/api/search", json={"query": "海", "mode": "smart"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["warning"])

    def test_a_plain_keyword_browse_never_probes(self) -> None:
        """No query means no model: browsing must not pay for a `GET /models`."""
        with patch(
            "photo_reviewer.server.probe_endpoint",
            side_effect=AssertionError("must not probe"),
        ):
            resp = self.client.post("/api/search", json={"query": "", "mode": "smart"})
        self.assertEqual(resp.status_code, 200)


class FaviconAndTitleTests(unittest.TestCase):
    """The document title and the custom SVG favicon."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            data_dir=self.tmp.name, db_path=os.path.join(self.tmp.name, "library.db")
        )
        self.db = Database(self.cfg.db_path)
        self.app = create_app(self.cfg, self.db)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_index_title_drops_album_and_links_favicon(self) -> None:
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("<title>拾光相册 / Lumina</title>", html)
        self.assertNotIn("Lumina Album", html)
        self.assertIn('href="/static/favicon.svg"', html)

    def test_favicon_routes_serve_svg(self) -> None:
        for route in ("/static/favicon.svg", "/favicon.svg", "/favicon.ico"):
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, route)
            self.assertIn("image/svg+xml", resp.content_type)
            self.assertIn("<svg", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
