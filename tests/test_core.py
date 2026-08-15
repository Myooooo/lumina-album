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
        with patch(
            "photo_reviewer.api_client.requests.post", side_effect=[bad, good]
        ) as post:
            result = analyze_image(self.image_path, self.cfg)
            self.assertEqual(result["score"], 7.5)
            self.assertEqual(post.call_count, 2)

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
        self.assertEqual(by_name["bad.jpg"]["status"], "error")
        self.assertIn("无法读取", by_name["bad.jpg"]["error"])


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

    def test_invalid_ids_return_400(self) -> None:
        self.assertEqual(
            self.client.post("/api/favorite", json={"id": "x"}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/reanalyze", json={"id": "x"}).status_code, 400
        )


if __name__ == "__main__":
    unittest.main()
