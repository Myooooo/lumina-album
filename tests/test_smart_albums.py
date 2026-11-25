"""Unit tests for Phase 4: cross-folder all-visible view & Virtual Smart Albums."""

from __future__ import annotations

import os
import tempfile
import unittest

from photo_reviewer.db import Database


class SmartAlbumsAndAllVisibleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "library.db"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, folder: str, name: str, score: float = 8.0, title: str = "") -> int:
        return self.db.upsert_photo(
            {
                "path": os.path.join(folder, name),
                "original_path": os.path.join(folder, name),
                "folder": folder,
                "filename": name,
                "title": title,
                "size": 1000,
                "width": 1000,
                "height": 800,
                "score": score,
                "status": "analyzed",
            }
        )

    def test_exclude_folders_keeps_hidden_albums_out_of_all_visible(self) -> None:
        self._add("/tmp/vis_a", "a.jpg", 9.0, "西湖落日")
        self._add("/tmp/vis_b", "b.jpg", 8.5, "京都红叶")
        self._add("/tmp/secret", "c.jpg", 9.5, "私密照")

        visible = self.db.list_photos(
            folder=None,
            exclude_deleted=True,
            exclude_folders=["/tmp/secret"],
            limit=50,
        )
        self.assertEqual(sorted(p["filename"] for p in visible), ["a.jpg", "b.jpg"])

        st = self.db.stats(folder=None, exclude_folders=["/tmp/secret"])
        self.assertEqual(st["total"], 2)

        # Keyword search also matches title now
        kw = self.db.list_photos(
            folder=None,
            search="京都",
            exclude_deleted=True,
            exclude_folders=["/tmp/secret"],
            limit=50,
        )
        self.assertEqual([p["filename"] for p in kw], ["b.jpg"])

    def test_smart_albums_crud(self) -> None:
        created = self.db.create_smart_album(
            "高分京都精选",
            {"folder": "__all__", "min_score": "8.5", "search": "京都", "search_mode": "keyword"},
        )
        self.assertEqual(created["name"], "高分京都精选")
        self.assertEqual(created["rules"]["min_score"], "8.5")

        listed = self.db.list_smart_albums()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], created["id"])

        self.assertTrue(self.db.delete_smart_album(created["id"]))
        self.assertEqual(self.db.list_smart_albums(), [])


if __name__ == "__main__":
    unittest.main()
