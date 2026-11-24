"""Unit tests for local People & Pets (face + cat/dog) recognition & clustering."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from photo_reviewer.db import Database
from photo_reviewer.faces import (
    active_accelerator_label,
    cluster_embeddings_dbscan,
    compute_centroid,
    cosine_distance,
    decode_embedding,
    encode_embedding,
    match_and_cluster_library,
    select_ort_providers,
)


class _DummyConfig:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.face_recognition_enabled = True
        self.face_auto_scan = True
        self.face_model = "buffalo_l"
        self.face_detect_pets = True
        self.face_match_threshold = 0.45
        self.face_min_cluster_size = 2
        self.face_models_dir = ""


class PeopleAndPetsTest(unittest.TestCase):
    def test_cross_platform_provider_selection(self) -> None:
        mac = select_ort_providers(
            available=["CoreMLExecutionProvider", "CPUExecutionProvider"],
            system_name="Darwin",
        )
        self.assertIsInstance(mac[0], tuple)
        self.assertEqual(mac[0][0], "CoreMLExecutionProvider")
        self.assertEqual(mac[0][1]["ModelFormat"], "MLProgram")
        self.assertEqual(active_accelerator_label(mac), "Apple CoreML (ANE / GPU)")

        win_cuda = select_ort_providers(
            available=["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"],
            system_name="Windows",
        )
        self.assertEqual(win_cuda[0], "CUDAExecutionProvider")
        self.assertEqual(win_cuda[1], "DmlExecutionProvider")
        self.assertEqual(active_accelerator_label(win_cuda), "NVIDIA CUDA GPU")

        win_dml = select_ort_providers(
            available=["DmlExecutionProvider", "CPUExecutionProvider"],
            system_name="Windows",
        )
        self.assertEqual(win_dml[0], "DmlExecutionProvider")
        self.assertEqual(active_accelerator_label(win_dml), "Windows DirectML GPU")

        linux_cuda = select_ort_providers(
            available=["CUDAExecutionProvider", "CPUExecutionProvider"],
            system_name="Linux",
        )
        self.assertEqual(linux_cuda[0], "CUDAExecutionProvider")

    def test_embedding_roundtrip_and_dbscan(self) -> None:
        v1 = [1.0, 0.0, 0.0, 0.0]
        v2 = [0.98, 0.1, 0.0, 0.0]
        v3 = [0.0, 0.0, 1.0, 0.0]
        v4 = [0.0, 0.05, 0.99, 0.0]
        noise = [0.0, 1.0, 0.0, 0.0]

        blob = encode_embedding(v1)
        decoded = decode_embedding(blob)
        self.assertIsNotNone(decoded)
        self.assertAlmostEqual(cosine_distance(decoded, v1), 0.0, places=5)

        labels = cluster_embeddings_dbscan([v1, v2, v3, v4, noise], eps=0.25, min_samples=2)
        self.assertEqual(labels[0], labels[1])
        self.assertEqual(labels[2], labels[3])
        self.assertNotEqual(labels[0], labels[2])
        self.assertEqual(labels[4], -1)

        centroid = compute_centroid([v1, v2])
        self.assertIsNotNone(centroid)
        self.assertLess(cosine_distance(centroid, v1), 0.05)

    def test_incremental_matching_pending_queue_and_pets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "library.db"))
            cfg = _DummyConfig(tmp)
            try:
                folder = str(Path(tmp) / "album")
                pids = []
                for idx in range(6):
                    pid = db.upsert_photo(
                        {
                            "path": f"{folder}/img_{idx}.jpg",
                            "folder": folder,
                            "filename": f"img_{idx}.jpg",
                            "status": "analyzed",
                        }
                    )
                    pids.append(pid)

                # Seed 2 faces of Alice, 2 detections of Cat Mochi, and 1 face of Bob.
                alice_a = encode_embedding([1.0, 0.0, 0.0, 0.0])
                alice_b = encode_embedding([0.99, 0.08, 0.0, 0.0])
                alice_pending = encode_embedding([0.48, 0.88, 0.0, 0.0])  # ~0.49 dist -> pending!
                cat_a = encode_embedding([0.0, 0.0, 1.0, 0.0])
                cat_b = encode_embedding([0.0, 0.04, 0.99, 0.0])
                stranger = encode_embedding([0.0, 1.0, 0.0, 0.0])

                db.replace_auto_faces_for_photo(
                    pids[0],
                    [{"entity_kind": "person", "bbox_x": 0.1, "bbox_y": 0.1, "bbox_w": 0.2, "bbox_h": 0.2, "det_score": 0.95, "embedding": alice_a}],
                    "buffalo_l",
                )
                db.replace_auto_faces_for_photo(
                    pids[1],
                    [{"entity_kind": "person", "bbox_x": 0.2, "bbox_y": 0.1, "bbox_w": 0.2, "bbox_h": 0.2, "det_score": 0.92, "embedding": alice_b}],
                    "buffalo_l",
                )
                db.replace_auto_faces_for_photo(
                    pids[2],
                    [{"entity_kind": "cat", "bbox_x": 0.3, "bbox_y": 0.3, "bbox_w": 0.3, "bbox_h": 0.3, "det_score": 0.88, "embedding": cat_a}],
                    "buffalo_l",
                )
                db.replace_auto_faces_for_photo(
                    pids[3],
                    [{"entity_kind": "cat", "bbox_x": 0.25, "bbox_y": 0.25, "bbox_w": 0.3, "bbox_h": 0.3, "det_score": 0.90, "embedding": cat_b}],
                    "buffalo_l",
                )
                db.replace_auto_faces_for_photo(
                    pids[4],
                    [{"entity_kind": "person", "bbox_x": 0.1, "bbox_y": 0.1, "bbox_w": 0.2, "bbox_h": 0.2, "det_score": 0.91, "embedding": stranger}],
                    "buffalo_l",
                )

                # Initial clustering should form 1 person cluster (Alice) + 1 cat cluster (Mochi).
                res1 = match_and_cluster_library(db, cfg)
                self.assertEqual(res1["clusters_created"], 2)

                people = db.list_people(folder=folder)
                self.assertEqual(len(people), 2)
                alice_row = next(p for p in people if p["kind"] == "person")
                cat_row = next(p for p in people if p["kind"] == "cat")
                self.assertEqual(alice_row["count"], 2)
                self.assertEqual(cat_row["count"], 2)

                # Name Alice & Mochi.
                db.rename_person(alice_row["id"], "Alice")
                db.rename_person(cat_row["id"], "Mochi")

                # New photo arrives with an auto-match face for Alice + a borderline pending face.
                db.replace_auto_faces_for_photo(
                    pids[5],
                    [
                        {"entity_kind": "person", "bbox_x": 0.1, "bbox_y": 0.1, "bbox_w": 0.2, "bbox_h": 0.2, "det_score": 0.93, "embedding": alice_a},
                        {"entity_kind": "person", "bbox_x": 0.5, "bbox_y": 0.1, "bbox_w": 0.2, "bbox_h": 0.2, "det_score": 0.82, "embedding": alice_pending},
                    ],
                    "buffalo_l",
                )
                res2 = match_and_cluster_library(db, cfg)
                self.assertEqual(res2["auto_matched"], 1)
                self.assertEqual(res2["pending_queued"], 1)

                # Photo detail returns both faces with person_name = Alice.
                detail_faces = db.faces_for_photo(pids[5])
                self.assertEqual(len(detail_faces), 2)
                self.assertEqual(detail_faces[0]["person_name"], "Alice")
                self.assertEqual(detail_faces[0]["status"], "auto")
                self.assertEqual(detail_faces[1]["status"], "pending")

                # Confirming pending face turns it into manual and increases confirmed photo count.
                pending = db.person_pending_faces(alice_row["id"], folder=folder)
                self.assertEqual(len(pending), 1)
                db.set_face_assignment(pending[0]["face_id"], alice_row["id"], status="manual")
                self.assertEqual(len(db.person_pending_faces(alice_row["id"], folder=folder)), 0)

                # Merge test: create a second person and merge into Alice.
                extra_id = db.create_person(name="Alice2", kind="person")
                db.merge_people([extra_id], alice_row["id"])
                self.assertIsNone(db.get_person(extra_id))
            finally:
                try:
                    db.close()
                except ImportError:
                    pass


    def test_manual_model_layout_detection_and_zip_extract(self) -> None:
        import zipfile
        from photo_reviewer.faces import (
            _extract_insightface_zip,
            engine_status,
            has_insightface_model,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _DummyConfig(tmp)
            # Manual placement directly under <models_dir>/buffalo_l/*.onnx should be detected.
            manual_dir = Path(tmp) / "models" / "buffalo_l"
            manual_dir.mkdir(parents=True, exist_ok=True)
            (manual_dir / "w600k_r50.onnx").write_bytes(b"fake-onnx")
            self.assertTrue(has_insightface_model(cfg, "buffalo_l"))

            st = engine_status(cfg)
            self.assertTrue(st["models_installed"]["buffalo_l"])
            self.assertIn("buffalo_l", st["manual_paths"])
            self.assertIn("buffalo_l", st["manual_urls"])

            # Zip extraction flattens nested folder into target_dir/*.onnx.
            zpath = Path(tmp) / "buffalo_s.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("buffalo_s/det_500m.onnx", b"onnx-det")
                zf.writestr("buffalo_s/w600k_mbf.onnx", b"onnx-rec")
            target = Path(tmp) / "models" / "models" / "buffalo_s"
            _extract_insightface_zip(zpath, target)
            self.assertTrue((target / "det_500m.onnx").is_file())
            self.assertTrue((target / "w600k_mbf.onnx").is_file())

    def test_live_periodic_clustering_during_scan_and_pet_download(self) -> None:
        import time
        from unittest import mock
        from photo_reviewer import faces as faces_mod

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "library.db"))
            cfg = _DummyConfig(tmp)
            folder = Path(tmp) / "album"
            folder.mkdir(parents=True, exist_ok=True)
            try:
                for idx in range(6):
                    img = folder / f"p_{idx}.jpg"
                    img.write_bytes(b"fake-jpg")
                    db.upsert_photo(
                        {
                            "path": str(img),
                            "folder": str(folder),
                            "filename": img.name,
                            "status": "analyzed",
                        }
                    )

                alice = [1.0, 0.0, 0.0, 0.0]
                det = [
                    faces_mod.DetectedEntity(
                        entity_kind="person",
                        bbox=(0.1, 0.1, 0.3, 0.3),
                        det_score=0.95,
                        embedding=alice,
                    )
                ]
                coord = faces_mod.PeopleScanCoordinator()
                with (
                    mock.patch.object(faces_mod.ENGINE, "detect", return_value=det),
                    mock.patch.object(faces_mod, "crop_entity_thumbnail", return_value=None),
                ):
                    coord.start(db, cfg, folder=str(folder))
                    deadline = time.monotonic() + 5.0
                    while coord.status()["status"] == "running" and time.monotonic() < deadline:
                        time.sleep(0.02)
                st = coord.status()
                self.assertEqual(st["status"], "completed")
                # Photo #5 triggers mid-scan cluster_version=1, final pass bumps to >=2.
                self.assertGreaterEqual(st["cluster_version"], 2)
                self.assertEqual(len(db.list_people(folder=str(folder))), 1)
            finally:
                try:
                    db.close()
                except ImportError:
                    pass

            # ModelDownloadCoordinator with include_pets=True invokes pet prepare with install_deps=True.
            face_dir = Path(tmp) / "models" / "models" / "buffalo_l"
            face_dir.mkdir(parents=True, exist_ok=True)
            (face_dir / "w600k_r50.onnx").write_bytes(b"fake-face")

            called: dict = {}

            def _fake_pet(root, install_deps=True, on_progress=None, on_status=None, cancel_event=None):
                called["install_deps"] = install_deps
                if on_status:
                    on_status("正在安装宠物识别依赖（ultralytics、onnx）…")
                    on_status("正在下载宠物检测权重（yolov8n.pt）…")
                if on_progress:
                    on_progress(50, 100, 100.0)
                if on_status:
                    on_status("正在导出宠物检测模型（yolov8n.onnx）…")
                (root / faces_mod.PET_DETECTOR_FILENAME).write_bytes(b"fake-pet-onnx")
                return None

            dl = faces_mod.ModelDownloadCoordinator()
            with mock.patch.object(faces_mod, "_try_prepare_pet_model", side_effect=_fake_pet):
                dl.start(cfg, model_name="buffalo_l", include_pets=True)
                deadline = time.monotonic() + 5.0
                while dl.status()["status"] == "running" and time.monotonic() < deadline:
                    time.sleep(0.02)
            dl_st = dl.status()
            self.assertEqual(dl_st["status"], "completed")
            self.assertTrue(called.get("install_deps"))
            self.assertIn("pets", dl_st["downloaded"])
            self.assertTrue(faces_mod.has_pet_model(cfg))


if __name__ == "__main__":
    unittest.main()
