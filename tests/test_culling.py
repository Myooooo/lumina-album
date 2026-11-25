"""Unit tests for Phase 3: photographer culling quality flags & Original+RAW export."""

from __future__ import annotations

import os
import tempfile
import unittest

from photo_reviewer.culling import classify_quality_flags, export_originals_to_dir


class CullingWorkflowTest(unittest.TestCase):
    def test_classify_quality_flags(self) -> None:
        self.assertEqual(classify_quality_flags(50.0, 0.02, 0.05), [])
        self.assertIn("疑似失焦", classify_quality_flags(10.0, 0.02, 0.05))
        self.assertIn("疑似过曝", classify_quality_flags(45.0, 0.25, 0.01))
        self.assertIn("疑似欠曝", classify_quality_flags(45.0, 0.01, 0.45))

    def test_export_originals_copy_and_hardlink_with_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = os.path.join(tmp, "src")
            out_copy = os.path.join(tmp, "out_copy")
            out_link = os.path.join(tmp, "out_link")
            os.makedirs(src_dir, exist_ok=True)

            jpg = os.path.join(src_dir, "DSC0001.JPG")
            arw = os.path.join(src_dir, "DSC0001.ARW")
            with open(jpg, "wb") as f:
                f.write(b"jpeg-bytes")
            with open(arw, "wb") as f:
                f.write(b"raw-bytes")

            photos = [{"id": 1, "path": jpg, "raw_path": arw}]
            r_copy = export_originals_to_dir(photos, out_copy, mode="copy", include_raw=True)
            self.assertEqual(r_copy["exported"], 1)
            self.assertEqual(r_copy["raw_exported"], 1)
            self.assertTrue(os.path.exists(os.path.join(out_copy, "DSC0001.JPG")))
            self.assertTrue(os.path.exists(os.path.join(out_copy, "DSC0001.ARW")))

            # Second copy into same folder uniquifies rather than overwriting
            r_copy2 = export_originals_to_dir(photos, out_copy, mode="copy", include_raw=False)
            self.assertEqual(r_copy2["exported"], 1)
            self.assertEqual(len(os.listdir(out_copy)), 3)

            r_link = export_originals_to_dir(photos, out_link, mode="hardlink", include_raw=True)
            self.assertEqual(r_link["exported"], 1)
            self.assertEqual(r_link["raw_exported"], 1)
            self.assertEqual(
                os.stat(jpg).st_ino,
                os.stat(os.path.join(out_link, "DSC0001.JPG")).st_ino,
            )


if __name__ == "__main__":
    unittest.main()
