"""Unit tests for Phase 1: local multimodal vector search, image similarity & vector duplicates."""

from __future__ import annotations

import os
import tempfile
import types
import unittest

from photo_reviewer.db import Database
from photo_reviewer.duplicates import group_duplicates_vector
from photo_reviewer.faces import encode_embedding
from photo_reviewer.vectors import (
    VISUAL_FALLBACK_MODEL,
    cosine_similarity,
    vector_engine_status,
    vector_image_search,
    vector_text_search,
)
from photo_reviewer.web.constants import SEARCH_MODES, _LEGACY_SEARCH_MODES


class VectorSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "library.db")
        self.db = Database(self.db_path)
        self.cfg = types.SimpleNamespace(
            data_dir=self._tmp.name,
            db_path=self.db_path,
            face_models_dir="",
            vector_search_enabled=True,
            vector_model="cn-clip-vit-b16",
            vector_min_similarity=0.22,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _insert_photo(self, name: str, folder: str = "/tmp/album") -> int:
        return self.db.upsert_photo(
            {
                "path": os.path.join(folder, name),
                "original_path": os.path.join(folder, name),
                "folder": folder,
                "filename": name,
                "size": 1024,
                "width": 1200,
                "height": 800,
                "status": "analyzed",
            }
        )

    def test_search_modes_preserve_keyword_and_add_vector(self) -> None:
        self.assertIn("keyword", SEARCH_MODES)
        self.assertIn("vector", SEARCH_MODES)
        self.assertIn("smart", SEARCH_MODES)
        self.assertIn("visual", SEARCH_MODES)
        self.assertNotIn("keyword", _LEGACY_SEARCH_MODES)

    def test_embeddings_crud_and_delete_cascade(self) -> None:
        pid1 = self._insert_photo("a.jpg")
        pid2 = self._insert_photo("b.jpg")
        v1 = [1.0, 0.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0, 0.0]
        b1 = encode_embedding(v1)
        b2 = encode_embedding(v2)
        assert b1 is not None and b2 is not None
        self.db.upsert_photo_embedding(pid1, VISUAL_FALLBACK_MODEL, 4, b1)
        self.db.upsert_photo_embedding(pid2, VISUAL_FALLBACK_MODEL, 4, b2)

        self.assertEqual(self.db.count_photo_embeddings(VISUAL_FALLBACK_MODEL), 2)
        embs = self.db.list_photo_embeddings(VISUAL_FALLBACK_MODEL, [pid1, pid2])
        self.assertEqual(set(embs.keys()), {pid1, pid2})

        self.db.delete_rows([pid1])
        self.assertEqual(self.db.count_photo_embeddings(VISUAL_FALLBACK_MODEL), 1)
        self.assertIsNone(self.db.get_photo_embedding(pid1, VISUAL_FALLBACK_MODEL))

    def test_vector_image_search_and_vector_duplicates(self) -> None:
        pid1 = self._insert_photo("sunset1.jpg")
        pid2 = self._insert_photo("sunset2.jpg")
        pid3 = self._insert_photo("forest.jpg")
        # Normalize vectors
        v_sun1 = [0.99, 0.141, 0.0, 0.0]
        v_sun2 = [0.985, 0.172, 0.0, 0.0]
        v_for = [0.0, 0.0, 1.0, 0.0]
        for pid, vec in ((pid1, v_sun1), (pid2, v_sun2), (pid3, v_for)):
            blob = encode_embedding(vec)
            assert blob is not None
            self.db.upsert_photo_embedding(pid, VISUAL_FALLBACK_MODEL, 4, blob)

        photos = self.db.list_photos(exclude_deleted=True, limit=50)
        target = next(p for p in photos if p["id"] == pid1)
        res = vector_image_search(self.db, self.cfg, target, photos, top_k=10, min_similarity=0.8)
        found_ids = [p["id"] for p in res["photos"]]
        self.assertEqual(found_ids, [pid2])

        groups = group_duplicates_vector(
            photos,
            {pid1: v_sun1, pid2: v_sun2, pid3: v_for},
            min_similarity=0.95,
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual([p["id"] for p in groups[0].photos], [pid1, pid2])

    def test_vector_text_search_graceful_when_clip_absent(self) -> None:
        pid = self._insert_photo("a.jpg")
        photos = self.db.list_photos(exclude_deleted=True, limit=10)
        res = vector_text_search(self.db, self.cfg, "夕阳海边", photos)
        self.assertFalse(res["clip_ready"])
        self.assertIn("尚未安装", res["warning"])
        st = vector_engine_status(self.db, self.cfg)
        self.assertEqual(st["total_photos"], 1)
        self.assertGreater(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 0.99)
        self.assertEqual(pid, photos[0]["id"])


if __name__ == "__main__":
    unittest.main()
