"""Regression tests for P0-P2 audit hardening (CLIP, hidden folders, export safety, batch cosine, stats projection)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from photo_reviewer import culling, db, stats_report, vectors
from photo_reviewer.faces import encode_embedding, l2_normalize


class ClipTokenizerAndSpecsTests(unittest.TestCase):
    def test_clip_download_specs_use_aligned_chinese_clip_onnx(self):
        specs = dict(vectors._clip_download_specs())
        self.assertIn("vocab.txt", specs)
        self.assertIn("vision.onnx", specs)
        self.assertIn("text.onnx", specs)
        self.assertTrue(specs["vocab.txt"][0].endswith("/OFA-Sys/chinese-clip-vit-base-patch16/resolve/main/vocab.txt"))
        self.assertTrue(specs["vision.onnx"][0].endswith("/felixdu/chinese-clip-vit-base-patch16-onnx/resolve/main/cn_clip_vision.onnx"))
        self.assertTrue(specs["text.onnx"][0].endswith("/felixdu/chinese-clip-vit-base-patch16-onnx/resolve/main/cn_clip_text.onnx"))

    def test_tokenize_clip_text_handles_cjk_and_english_wordpiece(self):
        vocab = {
            "[PAD]": 0,
            "[UNK]": 100,
            "[CLS]": 101,
            "[SEP]": 102,
            "日": 200,
            "落": 201,
            "sun": 300,
            "##set": 301,
        }
        ids, mask, types = vectors.tokenize_clip_text("日落 Sunset", vocab, max_len=10)
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(mask), 10)
        self.assertEqual(len(types), 10)
        # [CLS], 日, 落, sun, ##set, [SEP], [PAD]...
        self.assertEqual(ids[:6], [101, 200, 201, 300, 301, 102])
        self.assertEqual(mask[:6], [1, 1, 1, 1, 1, 1])
        self.assertEqual(mask[6:], [0, 0, 0, 0])
        self.assertEqual(types, [0] * 10)

    def test_batch_cosine_scores_matches_scalar_cosine(self):
        q = l2_normalize([1.0, 2.0, 3.0, 4.0])
        v1 = l2_normalize([1.0, 2.0, 3.0, 4.0])
        v2 = l2_normalize([-1.0, 0.0, 0.0, 0.0])
        v3 = l2_normalize([1.0, 1.8, 2.9, 4.1])
        blobs = [
            (1, encode_embedding(v1)),
            (2, encode_embedding(v2)),
            (3, encode_embedding(v3)),
            (4, b"bad"),  # wrong length -> skipped cleanly
        ]
        hits = dict((pid, sim) for sim, pid in vectors.batch_cosine_scores(q, blobs, threshold=0.9))
        self.assertIn(1, hits)
        self.assertIn(3, hits)
        self.assertNotIn(2, hits)
        self.assertNotIn(4, hits)
        self.assertAlmostEqual(hits[1], vectors.cosine_similarity(q, v1), places=5)
        self.assertAlmostEqual(hits[3], vectors.cosine_similarity(q, v3), places=5)


class HiddenFolderIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        self.database = db.Database(self.db_path)
        self.visible = os.path.join(self._tmp.name, "visible")
        self.hidden = os.path.join(self._tmp.name, "hidden")

        self.p1 = self.database.upsert_photo(
            {
                "path": os.path.join(self.visible, "a.jpg"),
                "folder": self.visible,
                "filename": "a.jpg",
                "status": "analyzed",
                "score": 8.8,
                "favorite": 1,
                "location": "杭州西湖",
                "tags": ["风景", "西湖"],
                "phash": "aaaaaaaaaaaaaaaa",
                "exif": {"datetime_original": "2023:05:20 10:00:00", "model": "Sony A7M4"},
            }
        )
        self.p2 = self.database.upsert_photo(
            {
                "path": os.path.join(self.hidden, "b.jpg"),
                "folder": self.hidden,
                "filename": "b.jpg",
                "status": "analyzed",
                "score": 9.2,
                "favorite": 1,
                "location": "私密地点",
                "tags": ["私密"],
                "phash": "aaaaaaaaaaaaaaaa",
                "exif": {"datetime_original": "2022:05:20 10:00:00", "model": "Leica Q3"},
            }
        )
        emb = encode_embedding(l2_normalize([1.0, 0.0, 0.0, 0.0]))
        self.database.upsert_photo_embedding(self.p1, "visual-hist-v1", 4, emb)
        self.database.upsert_photo_embedding(self.p2, "visual-hist-v1", 4, emb)

    def tearDown(self):
        for conn in list(getattr(self.database, "_connections", [])):
            try:
                import sqlite3
                sqlite3.Connection.close(conn)
            except Exception:
                pass
        self._tmp.cleanup()

    def test_exclude_folders_across_all_db_queries(self):
        ex = [self.hidden]
        self.assertEqual([p["id"] for p in self.database.list_photos(exclude_folders=ex)], [self.p1])
        self.assertEqual(self.database.stats(exclude_folders=ex)["total"], 1)
        self.assertEqual(self.database.all_tags(exclude_folders=ex), ["西湖", "风景"])
        self.assertEqual(self.database.capture_years(exclude_folders=ex), [2023])
        self.assertEqual(
            [p["id"] for p in self.database.memories_on(5, 20, window=0, exclude_folders=ex)],
            [self.p1],
        )
        self.assertEqual(
            [p["id"] for p in self.database.located_photos(exclude_folders=ex)],
            [self.p1],
        )
        self.assertEqual(
            [p["id"] for p in self.database.duplicate_candidates(exclude_folders=ex)],
            [self.p1],
        )
        self.assertEqual(
            self.database.count_photo_embeddings("visual-hist-v1", exclude_folders=ex),
            1,
        )
        report_rows = self.database.stats_report_rows(exclude_folders=ex)
        self.assertEqual([r["id"] for r in report_rows], [self.p1])
        rep = stats_report.build_shooting_report(report_rows)
        self.assertEqual(rep["summary"]["total"], 1)
        self.assertEqual([c["name"] for c in rep["cameras"]], ["Sony A7M4"])


class ExportOriginalsSafetyTests(unittest.TestCase):
    def test_blocks_system_root_and_internal_dirs(self):
        for bad in ("", "/", "/System/Library", "/etc", "/usr/local", "/tmp/album/.photo-review-cache/x", "/tmp/album/.photo-trash/2026"):
            with self.assertRaises(ValueError, msg=f"Should reject {bad!r}"):
                culling.validate_export_target_dir(bad)

    def test_blocks_excessive_batch_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "out")
            fake_photos = [{"id": i, "path": "/nonexistent.jpg"} for i in range(culling.MAX_ORIGINAL_EXPORT_PHOTOS + 1)]
            with self.assertRaises(ValueError):
                culling.export_originals_to_dir(fake_photos, target)

    def test_allows_safe_user_subdir_and_hardlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "img.jpg"
            src.write_bytes(b"fake-jpg-bytes")
            out_dir = Path(tmp) / "export_out"
            rep = culling.export_originals_to_dir(
                [{"id": 1, "path": str(src)}],
                str(out_dir),
                mode="hardlink",
                include_raw=False,
            )
            self.assertEqual(rep["exported"], 1)
            self.assertEqual(rep["mode"], "hardlink")
            self.assertTrue((out_dir / "img.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
