"""Local-first face & pet (cat/dog) detection, embedding, clustering and matching.

Everything runs strictly on the user's machine:
- Human faces: InsightFace ONNX packs (``buffalo_l`` default, ``buffalo_s`` optional)
- Pets (cats & dogs): Lightweight ONNX COCO detector (class 15 cat / 16 dog)
  paired with an ONNX visual embedding model (or a deterministic normalized
  patch-descriptor fallback when only the detector weights are present).
- Cross-platform acceleration via ONNX Runtime Execution Providers:
  * macOS (Apple Silicon / macOS 12+): CoreML EP (``MLProgram`` for FP32 fidelity) -> CPU
  * Windows: NVIDIA CUDA EP -> DirectML EP (AMD/Intel/NVIDIA) -> CPU
  * Linux: NVIDIA CUDA EP -> ROCm EP -> CPU

``onnxruntime``, ``numpy`` and ``insightface`` are optional dependencies so the
rest of Lumina Album keeps running even when they are not installed yet.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    requests = None  # type: ignore

logger = logging.getLogger(__name__)

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    np = None  # type: ignore

def _preload_nvidia_dlls() -> list[str]:
    """Make pip-installed CUDA/cuDNN DLLs discoverable on Windows.

    ``pip install nvidia-*-cu12`` places the DLLs under
    ``site-packages/nvidia/<component>/bin``. Windows does not search those
    directories automatically, so CUDAExecutionProvider would report itself as
    available and then fail to load. Python 3.8+ also ignores PATH changes for
    extension-module dependencies; the directories must be registered with
    ``os.add_dll_directory``.
    """
    if os.name != "nt":
        return []
    try:
        import site
        sites = []
        for entry in site.getsitepackages() + [site.getusersitepackages()]:
            if entry and entry not in sites:
                sites.append(entry)
    except Exception:
        return []
    directories: list[str] = []
    for entry in sites:
        nvidia = Path(entry) / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in sorted(nvidia.glob("*/bin")):
            if not bin_dir.is_dir():
                continue
            try:
                os.add_dll_directory(str(bin_dir))
            except OSError:
                continue
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            directories.append(str(bin_dir))
    return directories


_NVIDIA_DLL_DIRS = _preload_nvidia_dlls()

try:
    import onnxruntime as ort  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    ort = None  # type: ignore

try:
    from PIL import Image, ImageOps  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    Image = None  # type: ignore
    ImageOps = None  # type: ignore


FACE_MODELS = ("buffalo_l", "buffalo_s")

#: Official InsightFace release zips for one-click local model download.
INSIGHTFACE_MODEL_URLS = {
    "buffalo_l": (
        "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
    ),
    "buffalo_s": (
        "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip"
    ),
}

#: Lightweight YOLOv8n ONNX (COCO 80 classes; 15=cat, 16=dog) for local pet detection.
PET_DETECTOR_FILENAME = "yolov8n.onnx"
PET_WEIGHTS_FILENAME = "yolov8n.pt"
PET_DETECTOR_URL = (
    "https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.onnx"
)
PET_WEIGHTS_URL = (
    "https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.pt"
)

#: Periodic live clustering cadence during a background face/pet scan.
LIVE_CLUSTER_FIRST_PHOTO = 5
LIVE_CLUSTER_BATCH_PHOTOS = 15
LIVE_CLUSTER_INTERVAL_SEC = 4.0
COCO_CAT_CLASS = 15
COCO_DOG_CLASS = 16


# ---------------------------------------------------------------------------
# Cross-platform ONNX Runtime hardware acceleration
# ---------------------------------------------------------------------------


def select_ort_providers(available: list[str] | None = None, system_name: str | None = None) -> list[Any]:
    """Build an ordered ONNX Runtime provider list tailored to the host OS.

    On macOS, ``CoreMLExecutionProvider`` is configured with
    ``ModelFormat: MLProgram`` so CoreML preserves FP32 accuracy instead of
    silently downcasting face embeddings to FP16.
    """
    sys_name = (system_name or platform.system()).lower()
    if available is None:
        if ort is None:
            return []
        try:
            available = list(ort.get_available_providers())
        except Exception:
            available = ["CPUExecutionProvider"]

    avail_set = set(available)
    ordered: list[Any] = []

    if sys_name == "darwin":
        if "CoreMLExecutionProvider" in avail_set:
            ordered.append(
                (
                    "CoreMLExecutionProvider",
                    {
                        "ModelFormat": "MLProgram",
                        "MLComputeUnits": "ALL",
                    },
                )
            )
    elif sys_name == "windows":
        if "CUDAExecutionProvider" in avail_set:
            # EXHAUSTIVE is ORT's default and cost ~46 seconds on the first
            # Blackwell run while it tuned every convolution. HEURISTIC keeps
            # first-use latency under a second and is fine for these models.
            ordered.append(
                (
                    "CUDAExecutionProvider",
                    {"cudnn_conv_algo_search": "HEURISTIC"},
                )
            )
        if "DmlExecutionProvider" in avail_set:
            ordered.append("DmlExecutionProvider")
    else:
        # Linux and other POSIX hosts.
        if "CUDAExecutionProvider" in avail_set:
            ordered.append(
                (
                    "CUDAExecutionProvider",
                    {"cudnn_conv_algo_search": "HEURISTIC"},
                )
            )
        if "ROCMExecutionProvider" in avail_set:
            ordered.append("ROCMExecutionProvider")

    if "CPUExecutionProvider" in avail_set or not ordered:
        ordered.append("CPUExecutionProvider")
    return ordered


def active_accelerator_label(providers: list[Any] | None = None) -> str:
    """Human-readable label describing the top active hardware accelerator."""
    if ort is None and providers is None:
        return "未安装 onnxruntime"
    provs = providers if providers is not None else select_ort_providers()
    if not provs:
        return "CPU"
    first = provs[0][0] if isinstance(provs[0], tuple) else str(provs[0])
    mapping = {
        "CoreMLExecutionProvider": "Apple CoreML (ANE / GPU)",
        "CUDAExecutionProvider": "NVIDIA CUDA GPU",
        "DmlExecutionProvider": "Windows DirectML GPU",
        "ROCMExecutionProvider": "AMD ROCm GPU",
        "CPUExecutionProvider": "CPU",
    }
    return mapping.get(first, first)


# ---------------------------------------------------------------------------
# Embedding serialization & vector math (NumPy fast-path + pure-Python fallback)
# ---------------------------------------------------------------------------


def l2_normalize(vec: list[float]) -> list[float]:
    if not vec:
        return []
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-12:
        return [0.0 for _ in vec]
    inv = 1.0 / norm
    return [float(v * inv) for v in vec]


def encode_embedding(vec: Any) -> bytes | None:
    """Pack a float vector into compact little-endian float32 BLOB bytes."""
    if vec is None:
        return None
    if np is not None and isinstance(vec, np.ndarray):
        arr = vec.astype(np.float32, copy=False).ravel()
        norm = float(np.linalg.norm(arr))
        if norm > 1e-12:
            arr = arr / norm
        return arr.tobytes()
    values = l2_normalize([float(x) for x in vec])
    if not values:
        return None
    return struct.pack(f"<{len(values)}f", *values)


def decode_embedding(blob: bytes | None) -> list[float] | None:
    """Unpack a float32 BLOB back into a Python list of floats."""
    if not blob:
        return None
    if len(blob) % 4 != 0:
        return None
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance ``1 - cos(a, b)`` in ``[0, 2]`` for normalized vectors."""
    if not a or not b or len(a) != len(b):
        return 1.0
    dot = sum(x * y for x, y in zip(a, b))
    dot = max(-1.0, min(1.0, dot))
    return 1.0 - dot


def compute_centroid(embeddings: list[list[float]]) -> list[float] | None:
    """Compute the L2-normalized mean embedding for a person or pet."""
    valid = [e for e in embeddings if e]
    if not valid:
        return None
    dim = len(valid[0])
    valid = [e for e in valid if len(e) == dim]
    if not valid:
        return None
    if np is not None:
        mat = np.asarray(valid, dtype=np.float32)
        mean = mat.mean(axis=0)
        norm = float(np.linalg.norm(mean))
        if norm > 1e-12:
            mean = mean / norm
        return mean.astype(np.float32).tolist()
    sums = [0.0] * dim
    for vec in valid:
        for i, val in enumerate(vec):
            sums[i] += val
    return l2_normalize(sums)


# ---------------------------------------------------------------------------
# Density-based clustering (DBSCAN on cosine distance)
# ---------------------------------------------------------------------------


def cluster_embeddings_dbscan(
    embeddings: list[list[float]],
    eps: float = 0.48,
    min_samples: int = 2,
) -> list[int]:
    """Cluster normalized embeddings using DBSCAN over cosine distance.

    Returns a list of cluster labels of length ``len(embeddings)``, where
    ``-1`` means noise (singleton / unclustered) and ``0, 1, ...`` are cluster
    indices. Uses NumPy matrix multiplication when installed so thousands of
    embeddings cluster in milliseconds.
    """
    n = len(embeddings)
    if n == 0:
        return []
    min_pts = max(1, int(min_samples))

    # Build neighbor lists using cosine distance <= eps.
    neighbors: list[list[int]] = [[] for _ in range(n)]
    if np is not None:
        mat = np.asarray(embeddings, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.where(norms > 1e-12, norms, 1.0)
        mat = mat / norms
        # Chunked similarity matrix to keep peak memory small on 20k+ faces.
        sim_thresh = 1.0 - float(eps)
        chunk = 512
        for start in range(0, n, chunk):
            end = min(n, start + chunk)
            sims = mat[start:end] @ mat.T
            for offset in range(end - start):
                idx = start + offset
                hits = np.flatnonzero(sims[offset] >= sim_thresh)
                neighbors[idx] = hits.tolist()
    else:
        for i in range(n):
            neighbors[i].append(i)
            for j in range(i + 1, n):
                if cosine_distance(embeddings[i], embeddings[j]) <= eps:
                    neighbors[i].append(j)
                    neighbors[j].append(i)

    labels = [-1] * n
    visited = [False] * n
    cluster_id = 0

    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        nbrs = neighbors[i]
        if len(nbrs) < min_pts:
            continue
        labels[i] = cluster_id
        queue = list(nbrs)
        head = 0
        in_queue = set(nbrs)
        while head < len(queue):
            q = queue[head]
            head += 1
            if not visited[q]:
                visited[q] = True
                q_nbrs = neighbors[q]
                if len(q_nbrs) >= min_pts:
                    for nxt in q_nbrs:
                        if nxt not in in_queue:
                            in_queue.add(nxt)
                            queue.append(nxt)
            if labels[q] == -1:
                labels[q] = cluster_id
        cluster_id += 1

    return labels


# ---------------------------------------------------------------------------
# Model directory & local availability helpers
# ---------------------------------------------------------------------------


def models_root(cfg) -> Path:
    custom = str(getattr(cfg, "face_models_dir", "") or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return (Path(cfg.data_dir) / "models").resolve()


def face_thumbs_dir(cfg, folder: str | None = None) -> Path:
    """Return the thumbnail directory for faces in one album.

    Face/pet crops belong with the album they came from, inside its hidden
    cache directory, not in the project's data directory. Keeping them there
    also means removing or hiding an album takes its entity thumbnails with it.
    ``folder`` is optional only for legacy callers that have no album context;
    normal scans and API reads always pass it.
    """
    if folder:
        d = Path(folder) / getattr(cfg, "cache_dir_name", ".photo-review-cache") / "face_thumbs"
    else:
        d = Path(cfg.data_dir) / "face_thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def migrate_face_thumbnails(database, cfg) -> int:
    """Move legacy central face crops into their album cache directories.

    Older builds wrote every crop to ``data/face_thumbs``. The library is
    re-homed lazily too (the thumb route migrates the one it is asked for), but
    a startup pass keeps the old directory from lingering after a restart.
    Missing source files or disconnected drives are skipped: a thumbnail is
    regenerated on demand.
    """
    moved = 0
    try:
        rows = database.faces_with_thumb_paths()
    except Exception:
        return 0
    for row in rows:
        old = row.get("thumb_path") or ""
        if not old or not os.path.isfile(old):
            continue
        folder = row.get("folder") or ""
        if not folder:
            continue
        target = face_thumbs_dir(cfg, folder) / f"face_{int(row['id'])}.jpg"
        if os.path.abspath(old) == os.path.abspath(str(target)):
            continue
        try:
            if target.exists():
                os.remove(old)
            else:
                shutil.move(old, target)
            database.update_face_thumb(int(row["id"]), str(target))
            moved += 1
        except OSError:
            logger.debug("could not move face thumb %s", old, exc_info=True)
    return moved


def _find_insightface_model_dir(cfg, model_name: str) -> Path | None:
    """Locate an installed InsightFace pack, tolerating manual zip layouts."""
    root = models_root(cfg)
    candidates = (
        root / "models" / model_name,
        root / "models" / model_name / model_name,
        root / model_name,
        root / model_name / model_name,
    )
    for cand in candidates:
        if cand.is_dir() and any(cand.glob("*.onnx")):
            return cand
    return None


def _normalize_insightface_layout(cfg, model_name: str) -> bool:
    """Ensure ONNX files sit in ``<models_dir>/models/<model_name>/`` for InsightFace."""
    target = models_root(cfg) / "models" / model_name
    if target.is_dir() and any(target.glob("*.onnx")):
        return True
    found = _find_insightface_model_dir(cfg, model_name)
    if found is None:
        return False
    if found.resolve() == target.resolve():
        return True
    target.mkdir(parents=True, exist_ok=True)
    for onnx_file in found.glob("*.onnx"):
        dest = target / onnx_file.name
        if not dest.exists():
            shutil.copy2(onnx_file, dest)
    return any(target.glob("*.onnx"))


def has_insightface_model(cfg, model_name: str) -> bool:
    return _find_insightface_model_dir(cfg, model_name) is not None


def _find_pet_model_path(cfg) -> Path | None:
    root = models_root(cfg)
    for cand in (root / PET_DETECTOR_FILENAME, root / "models" / PET_DETECTOR_FILENAME):
        if cand.is_file() and cand.stat().st_size > 0:
            return cand
    return None


def has_pet_model(cfg) -> bool:
    return _find_pet_model_path(cfg) is not None


def engine_status(cfg) -> dict[str, Any]:
    """Report runtime capabilities, hardware provider, and local model status."""
    model_name = getattr(cfg, "face_model", "buffalo_l")
    if model_name not in FACE_MODELS:
        model_name = "buffalo_l"
    has_insightface_pkg = False
    try:
        import insightface  # type: ignore  # noqa: F401

        has_insightface_pkg = True
    except ImportError:
        has_insightface_pkg = False

    has_ultralytics_pkg = importlib.util.find_spec("ultralytics") is not None
    providers = select_ort_providers()
    ort_available = ort is not None
    try:
        runtime_device = str(ort.get_device()) if ort is not None else ""
    except Exception:
        runtime_device = ""
    runtime_accelerator = active_accelerator_label(providers)
    if (
        providers
        and (providers[0][0] if isinstance(providers[0], tuple) else providers[0])
        == "CUDAExecutionProvider"
        and runtime_device
        and runtime_device.upper() != "GPU"
    ):
        runtime_accelerator = "NVIDIA CUDA 未加载（回退 CPU）"
    numpy_available = np is not None
    pillow_available = Image is not None
    deps = {
        "onnxruntime": ort_available,
        "numpy": numpy_available,
        "insightface": has_insightface_pkg,
        "pillow": pillow_available,
        "ultralytics": has_ultralytics_pkg,
    }
    missing_packages = [
        k for k in ("onnxruntime", "numpy", "insightface", "pillow") if not deps[k]
    ]
    models_installed = {
        "buffalo_l": has_insightface_model(cfg, "buffalo_l"),
        "buffalo_s": has_insightface_model(cfg, "buffalo_s"),
        "pets": has_pet_model(cfg),
    }
    face_ready = (
        ort_available
        and numpy_available
        and pillow_available
        and has_insightface_pkg
        and models_installed.get(model_name, False)
    )
    pet_ready = (
        ort_available
        and numpy_available
        and pillow_available
        and models_installed["pets"]
    )
    root = models_root(cfg)
    return {
        "enabled": bool(getattr(cfg, "face_recognition_enabled", True)),
        "auto_scan": bool(getattr(cfg, "face_auto_scan", True)),
        "model": model_name,
        "detect_pets": bool(getattr(cfg, "face_detect_pets", True)),
        "models_dir": str(root),
        "manual_paths": {
            "buffalo_l": str(root / "models" / "buffalo_l"),
            "buffalo_s": str(root / "models" / "buffalo_s"),
            "pets": str(root / PET_DETECTOR_FILENAME),
        },
        "manual_urls": {
            "buffalo_l": INSIGHTFACE_MODEL_URLS["buffalo_l"],
            "buffalo_s": INSIGHTFACE_MODEL_URLS["buffalo_s"],
        },
        "platform": platform.system(),
        "accelerator": runtime_accelerator,
        "runtime_device": runtime_device,
        "nvidia_dll_dirs": list(_NVIDIA_DLL_DIRS),
        "providers": [p[0] if isinstance(p, tuple) else p for p in providers],
        "deps": deps,
        "missing_packages": missing_packages,
        "models_installed": models_installed,
        "face_ready": face_ready,
        "pet_ready": pet_ready,
        "ready": face_ready or (bool(getattr(cfg, "face_detect_pets", True)) and pet_ready),
        "engine_ready": face_ready or (bool(getattr(cfg, "face_detect_pets", True)) and pet_ready),
    }


def _extract_insightface_zip(zip_path: Path, target_dir: Path) -> None:
    """Extract a buffalo_l/buffalo_s zip so all .onnx files land directly in target_dir."""
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            fname = Path(member.filename).name
            if not fname or fname.startswith(".") or "__MACOSX" in member.filename:
                continue
            if not fname.lower().endswith(".onnx"):
                continue
            dest = target_dir / fname
            with zf.open(member, "r") as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _stream_download_file(
    url: str,
    dest_path: Path,
    on_progress=None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Download ``url`` to ``dest_path`` in chunks with progress & cancel support."""
    part_path = dest_path.with_suffix(dest_path.suffix + ".part")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Lumina-Album/1.0"}
    downloaded = 0
    total = 0
    started = time.monotonic()

    try:
        if requests is not None:
            with requests.get(url, stream=True, timeout=(15, 60), headers=headers) as resp:
                resp.raise_for_status()
                raw_len = resp.headers.get("Content-Length")
                total = int(raw_len) if raw_len and raw_len.isdigit() else 0
                if on_progress:
                    on_progress(0, total, 0.0)
                with open(part_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if cancel_event is not None and cancel_event.is_set():
                            raise InterruptedError("已取消下载")
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        elapsed = max(0.05, time.monotonic() - started)
                        if on_progress:
                            on_progress(downloaded, total, downloaded / elapsed)
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw_len = resp.headers.get("Content-Length")
                total = int(raw_len) if raw_len and raw_len.isdigit() else 0
                if on_progress:
                    on_progress(0, total, 0.0)
                with open(part_path, "wb") as f:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise InterruptedError("已取消下载")
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        elapsed = max(0.05, time.monotonic() - started)
                        if on_progress:
                            on_progress(downloaded, total, downloaded / elapsed)
        os.replace(part_path, dest_path)
    except Exception:
        if part_path.exists():
            part_path.unlink(missing_ok=True)
        raise


def _install_pet_dependencies(
    cancel_event: threading.Event | None = None,
    on_status=None,
) -> str | None:
    """Install ``ultralytics`` and ``onnx`` via pip when missing for pet detection."""
    missing = [
        pkg for pkg in ("ultralytics", "onnx") if importlib.util.find_spec(pkg) is None
    ]
    if not missing:
        return None
    if on_status:
        on_status(f"正在安装宠物识别依赖（{'、'.join(missing)}）…")
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", *missing]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise InterruptedError("已取消下载")
            time.sleep(0.2)
        output = ""
        if proc.stdout is not None:
            output = (proc.stdout.read() or "").strip()
        if proc.returncode != 0:
            tail = output.splitlines()[-1] if output else f"退出码 {proc.returncode}"
            return (
                f"宠物识别依赖（{'、'.join(missing)}）自动安装失败：{tail}。"
                "可手动执行 pip install ultralytics onnx 后重试，或直接放入 yolov8n.onnx。"
            )
        importlib.invalidate_caches()
        return None
    except InterruptedError:
        raise
    except Exception as exc:
        return f"宠物识别依赖自动安装异常（不影响人脸识别）：{exc}"


def _try_prepare_pet_model(
    root: Path,
    install_deps: bool = True,
    on_progress=None,
    on_status=None,
    cancel_event: threading.Event | None = None,
) -> str | None:
    """Install pet deps if requested, download yolov8n.pt and export yolov8n.onnx."""
    dest = root / PET_DETECTOR_FILENAME
    if dest.is_file() and dest.stat().st_size > 0:
        return None
    if install_deps:
        dep_err = _install_pet_dependencies(
            cancel_event=cancel_event, on_status=on_status
        )
        if dep_err:
            return dep_err
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("已取消下载")
    try:
        from ultralytics import YOLO  # type: ignore
    except ImportError:
        return (
            "未检测到 ultralytics：请安装 ultralytics onnx，"
            "或手动将 yolov8n.onnx 放入模型目录。"
        )

    pt_path = root / PET_WEIGHTS_FILENAME
    downloaded_pt = False
    try:
        if not (pt_path.is_file() and pt_path.stat().st_size > 0):
            if on_status:
                on_status("正在下载宠物检测权重（yolov8n.pt）…")
            _stream_download_file(
                PET_WEIGHTS_URL,
                pt_path,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            downloaded_pt = True
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("已取消下载")
        if on_status:
            on_status("正在导出宠物检测模型（yolov8n.onnx）…")
        model = YOLO(str(pt_path))
        exported = model.export(format="onnx", imgsz=640, simplify=False)
        if exported and os.path.isfile(str(exported)):
            if Path(str(exported)).resolve() != dest.resolve():
                shutil.move(str(exported), str(dest))
            if downloaded_pt and pt_path.exists():
                pt_path.unlink(missing_ok=True)
            return None
    except InterruptedError:
        raise
    except Exception as exc:
        return f"宠物检测模型自动准备未完成（不影响人脸识别）：{exc}"
    return "宠物检测模型尚未就绪，可手动放入 yolov8n.onnx。"


@dataclass
class ModelDownloadJob:
    status: str = "idle"  # idle | running | completed | cancelled | error
    model: str = "buffalo_l"
    include_pets: bool = True
    stage: str = "idle"  # idle | deps | runtime | downloading | extracting | pets | done
    current_item: str = ""
    command: str = ""
    downloaded_bytes: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    speed_bps: float = 0.0
    message: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model": self.model,
            "include_pets": self.include_pets,
            "stage": self.stage,
            "current_item": self.current_item,
            "command": self.command,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "percent": round(self.percent, 1),
            "speed_bps": round(self.speed_bps, 1),
            "message": self.message,
            "error": self.error,
            "warnings": list(self.warnings),
            "downloaded": list(self.downloaded),
        }


def _has_nvidia_gpu() -> bool:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        result = subprocess.run(
            [exe, "-L"], capture_output=True, text=True, timeout=8
        )
        return result.returncode == 0 and "GPU" in (result.stdout or "")
    except Exception:
        return False


def _pip_install(job: "ModelDownloadJob", args: list[str], stage: str, message: str) -> str | None:
    """Run one pip command and fold its result into the download job."""
    job.stage = stage
    job.message = message
    job.command = f"{sys.executable} -m pip " + " ".join(args)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", *args, "--disable-pip-version-check"],
            capture_output=True,
            text=True,
            timeout=2400,
        )
    except Exception as exc:
        return str(exc)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        return tail[0] if tail else f"pip 退出码 {proc.returncode}"
    return None


def _ensure_face_runtime(job: "ModelDownloadJob", include_pets: bool) -> str | None:
    """Install InsightFace and the best ONNX Runtime for this machine.

    The button is intentionally one action: dependencies, the GPU/CPU runtime
    and the model files. The actual runtime swap cannot take effect in the
    already-running process, so the job tells the UI that a restart is needed.
    """
    import importlib.util

    job.percent = max(job.percent, 2)
    missing = [
        name
        for name in ("numpy", "insightface", "onnx", "PIL")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        pkg = ["numpy", "insightface", "onnx", "opencv-python-headless"]
        err = _pip_install(job, ["install", *pkg], "deps", "正在安装 InsightFace 与人像依赖…")
        job.percent = 16
        if err:
            return f"人像依赖安装失败：{err}"

    active_cuda = False
    try:
        if ort is not None:
            providers = list(ort.get_available_providers())
            active_cuda = "CUDAExecutionProvider" in providers and str(ort.get_device()).upper() == "GPU"
    except Exception:
        active_cuda = False

    if not active_cuda:
        if _has_nvidia_gpu():
            # Avoid the known CPU-wheel-overwrites-GPU-wheel trap: remove the
            # CPU package first, then install the GPU wheel without letting pip
            # resolve insightface's onnxruntime dependency again.
            _pip_install(job, ["uninstall", "-y", "onnxruntime"], "runtime", "正在移除 CPU 版 ONNX Runtime…")
            version = "onnxruntime-gpu==1.19.2" if sys.version_info < (3, 10) else "onnxruntime-gpu"
            err = _pip_install(
                job,
                ["install", "--no-deps", "--force-reinstall", version],
                "runtime",
                "正在安装 NVIDIA CUDA 版 ONNX Runtime…",
            )
            job.percent = 24
            if err:
                return f"CUDA 版 ONNX Runtime 安装失败：{err}。可先重启应用后重试。"
            # cuDNN 9 / CUDA 12 DLLs. These wheels are what let ORT 1.19 load
            # on Blackwell without a system-wide CUDA 12 install.
            err = _pip_install(
                job,
                [
                    "install",
                    "nvidia-cuda-runtime-cu12",
                    "nvidia-cublas-cu12",
                    "nvidia-cufft-cu12",
                    "nvidia-cudnn-cu12",
                ],
                "runtime",
                "正在安装 CUDA 12 / cuDNN 9 运行库…",
            )
            job.percent = 34
            if err:
                return f"CUDA 运行库安装失败：{err}"
            job.command = ""
            job.warnings.append("CUDA 版 ONNX Runtime 已安装，重启应用后生效。")
            job.message = "人像运行时已安装，重启应用后生效；接下来下载模型。"
        elif sys.platform == "win32":
            _pip_install(job, ["uninstall", "-y", "onnxruntime"], "runtime", "正在切换 Windows 图形后端…")
            err = _pip_install(
                job,
                ["install", "--no-deps", "--force-reinstall", "onnxruntime-directml"],
                "runtime",
                "正在安装 Windows DirectML 版 ONNX Runtime…",
            )
            job.percent = 34
            if err:
                return f"DirectML 版 ONNX Runtime 安装失败：{err}"
        else:
            err = _pip_install(job, ["install", "onnxruntime"], "runtime", "正在安装 ONNX Runtime…")
            job.percent = 34
            if err:
                return f"ONNX Runtime 安装失败：{err}"

    if include_pets and importlib.util.find_spec("ultralytics") is None:
        job.message = "正在检查宠物识别依赖…"
        err = _pip_install(
            job,
            ["install", "ultralytics", "onnx"],
            "pets",
            "正在安装宠物识别依赖 ultralytics / onnx…",
        )
        job.percent = max(job.percent, 40)
        if err:
            return f"宠物识别依赖安装失败：{err}（不影响人像识别）"
    importlib.invalidate_caches()
    return None


class ModelDownloadCoordinator:
    """Background streaming downloader with live progress for local face/pet models."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job = ModelDownloadJob()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._job.to_dict()

    def cancel(self) -> bool:
        with self._lock:
            if self._job.status == "running":
                self._job.cancel_event.set()
                return True
            return False

    def start(
        self,
        cfg,
        model_name: str = "buffalo_l",
        include_pets: bool = True,
        install_deps: bool = True,
    ) -> dict[str, Any]:
        if model_name not in FACE_MODELS:
            raise ValueError(f"不支持的模型名称：{model_name}")
        with self._lock:
            if self._job.status == "running":
                return self._job.to_dict()
            job = ModelDownloadJob(
                status="running",
                model=model_name,
                include_pets=bool(include_pets),
                stage="downloading",
                current_item=f"{model_name}.zip",
                message=f"正在准备下载 {model_name} 模型…",
            )
            self._job = job

        def _worker():
            try:
                root = models_root(cfg)
                root.mkdir(parents=True, exist_ok=True)
                need_pets = bool(include_pets and not has_pet_model(cfg))
                face_max_pct = 62.0 if need_pets else 92.0
                install_note = None
                if install_deps:
                    install_note = _ensure_face_runtime(job, include_pets)
                    if install_note:
                        job.warnings.append(install_note)

                if not has_insightface_model(cfg, model_name):
                    zip_path = root / f"{model_name}.zip"
                    target_dir = root / "models" / model_name
                    url = INSIGHTFACE_MODEL_URLS[model_name]
                    job.stage = "downloading"
                    job.current_item = f"{model_name}.zip"
                    job.message = f"正在下载 {model_name}.zip…"

                    def _on_prog(done_b: int, total_b: int, speed: float):
                        job.downloaded_bytes = done_b
                        job.total_bytes = total_b
                        job.speed_bps = speed
                        if total_b > 0:
                            job.percent = max(job.percent, min(face_max_pct, (done_b / total_b) * face_max_pct))

                    _stream_download_file(
                        url,
                        zip_path,
                        on_progress=_on_prog,
                        cancel_event=job.cancel_event,
                    )
                    job.stage = "extracting"
                    job.percent = max(job.percent, face_max_pct + 2.0)
                    job.message = f"下载完成，正在解压 {model_name} 模型…"
                    try:
                        _extract_insightface_zip(zip_path, target_dir)
                    finally:
                        if zip_path.exists():
                            zip_path.unlink(missing_ok=True)
                    if not has_insightface_model(cfg, model_name):
                        raise RuntimeError(f"解压 {model_name}.zip 后未找到 .onnx 模型文件")
                    job.downloaded.append(model_name)
                else:
                    _normalize_insightface_layout(cfg, model_name)
                    job.percent = 12.0 if need_pets else 92.0
                    job.message = f"{model_name} 模型已存在于本地"

                if need_pets:
                    if job.cancel_event.is_set():
                        raise InterruptedError("已取消下载")
                    pet_base = job.percent
                    job.stage = "pets"
                    job.current_item = "ultralytics / yolov8n.onnx"
                    job.downloaded_bytes = 0
                    job.total_bytes = 0
                    job.speed_bps = 0.0

                    def _on_pet_status(msg: str):
                        job.message = msg
                        if "安装宠物识别依赖" in msg:
                            job.stage = "pets_deps"
                            job.current_item = "pip install ultralytics onnx"
                            job.percent = max(job.percent, min(72.0, pet_base + 12.0))
                        elif "下载宠物检测权重" in msg:
                            job.stage = "pets"
                            job.current_item = PET_WEIGHTS_FILENAME
                            job.percent = max(job.percent, 75.0)
                        elif "导出宠物检测模型" in msg:
                            job.stage = "pets_export"
                            job.current_item = PET_DETECTOR_FILENAME
                            job.downloaded_bytes = 0
                            job.total_bytes = 0
                            job.speed_bps = 0.0
                            job.percent = max(job.percent, 94.0)

                    def _on_pet_prog(done_b: int, total_b: int, speed: float):
                        job.downloaded_bytes = done_b
                        job.total_bytes = total_b
                        job.speed_bps = speed
                        if total_b > 0:
                            job.percent = max(job.percent, min(93.0, 75.0 + (done_b / total_b) * 18.0))

                    warn = _try_prepare_pet_model(
                        root,
                        install_deps=True,
                        on_progress=_on_pet_prog,
                        on_status=_on_pet_status,
                        cancel_event=job.cancel_event,
                    )
                    if warn:
                        job.warnings.append(warn)
                    else:
                        job.downloaded.append("pets")

                job.stage = "done"
                job.percent = 100.0
                job.status = "completed"
                if job.downloaded:
                    job.message = f"本地模型准备完成：{', '.join(job.downloaded)}"
                else:
                    job.message = f"本地模型（{model_name}）已就绪，无需重复下载"
            except InterruptedError:
                job.status = "cancelled"
                job.message = "已取消模型下载"
            except Exception as exc:
                logger.warning("model download failed: %s", exc, exc_info=True)
                job.status = "error"
                job.error = str(exc)
                job.message = f"下载失败：{exc}（可按设置页指引手动下载并放到模型目录）"

        t = threading.Thread(target=_worker, name="lumina-model-download", daemon=True)
        t.start()
        return job.to_dict()


MODEL_DOWNLOADER = ModelDownloadCoordinator()


def download_local_models(
    cfg,
    model_name: str = "buffalo_l",
    include_pets: bool = True,
) -> dict[str, Any]:
    """Download InsightFace ONNX pack and optional pet detector to local models_dir."""
    if model_name not in FACE_MODELS:
        raise ValueError(f"不支持的模型名称：{model_name}")
    root = models_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    warnings: list[str] = []

    if not has_insightface_model(cfg, model_name):
        zip_path = root / f"{model_name}.zip"
        target_dir = root / "models" / model_name
        url = INSIGHTFACE_MODEL_URLS[model_name]
        logger.info("downloading %s from %s", model_name, url)
        _stream_download_file(url, zip_path)
        try:
            _extract_insightface_zip(zip_path, target_dir)
        finally:
            if zip_path.exists():
                zip_path.unlink(missing_ok=True)
        downloaded.append(model_name)
    else:
        _normalize_insightface_layout(cfg, model_name)

    if include_pets and not has_pet_model(cfg):
        warn = _try_prepare_pet_model(root)
        if warn:
            warnings.append(warn)
        else:
            downloaded.append("pets")

    return {"downloaded": downloaded, "warnings": warnings, "status": engine_status(cfg)}


# ---------------------------------------------------------------------------
# Face & pet thumbnail cropping
# ---------------------------------------------------------------------------


def crop_entity_thumbnail(
    image_path: str,
    bbox: tuple[float, float, float, float],
    output_path: Path,
    size: int = 240,
    pad_ratio: float = 0.35,
) -> str | None:
    """Crop a square avatar thumbnail around a normalized ``(x, y, w, h)`` box."""
    if Image is None:
        return None
    try:
        with Image.open(image_path) as raw:
            img = ImageOps.exif_transpose(raw).convert("RGB")
            iw, ih = img.size
            if iw <= 0 or ih <= 0:
                return None
            bx, by, bw, bh = bbox
            cx = (bx + bw * 0.5) * iw
            cy = (by + bh * 0.5) * ih
            side = max(bw * iw, bh * ih) * (1.0 + pad_ratio * 2.0)
            side = max(32.0, min(side, float(min(iw, ih))))
            half = side * 0.5
            x0 = max(0.0, min(iw - side, cx - half))
            y0 = max(0.0, min(ih - side, cy - half))
            crop = img.crop((int(x0), int(y0), int(x0 + side), int(y0 + side)))
            crop = crop.resize((size, size), Image.Resampling.LANCZOS)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            crop.save(output_path, format="JPEG", quality=86)
            return str(output_path)
    except Exception:
        logger.debug("failed cropping face/pet thumb for %s", image_path, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Local inference engine (InsightFace ONNX + YOLOv8n Pet ONNX)
# ---------------------------------------------------------------------------


@dataclass
class DetectedEntity:
    entity_kind: str  # "person" | "cat" | "dog"
    bbox: tuple[float, float, float, float]  # normalized (x, y, w, h) in [0, 1]
    det_score: float
    embedding: list[float]


def _extract_pet_patch_embedding(img_rgb: Any, bbox: tuple[float, float, float, float]) -> list[float]:
    """Deterministic local visual descriptor for a cropped cat/dog region.

    Produces a normalized 128-d color-and-spatial histogram vector from the pet
    crop so cats and dogs can be clustered locally without requiring a second
    large neural network.
    """
    if Image is None or np is None:
        return []
    iw, ih = img_rgb.size
    x, y, w, h = bbox
    x0 = max(0, int(x * iw))
    y0 = max(0, int(y * ih))
    x1 = min(iw, max(x0 + 1, int((x + w) * iw)))
    y1 = min(ih, max(y0 + 1, int((y + h) * ih)))
    crop = img_rgb.crop((x0, y0, x1, y1)).resize((64, 64), Image.Resampling.BILINEAR)
    hsv = np.asarray(crop.convert("HSV"), dtype=np.float32)
    # 2x2 spatial grid x (16 H bins + 8 S bins + 8 V bins) = 128-d descriptor.
    feats: list[float] = []
    for gy in (0, 32):
        for gx in (0, 32):
            patch = hsv[gy : gy + 32, gx : gx + 32]
            h_hist, _ = np.histogram(patch[:, :, 0], bins=16, range=(0, 256))
            s_hist, _ = np.histogram(patch[:, :, 1], bins=8, range=(0, 256))
            v_hist, _ = np.histogram(patch[:, :, 2], bins=8, range=(0, 256))
            feats.extend(h_hist.astype(np.float32).tolist())
            feats.extend(s_hist.astype(np.float32).tolist())
            feats.extend(v_hist.astype(np.float32).tolist())
    return l2_normalize(feats)


def _nms_boxes(boxes: list[tuple[float, float, float, float, float, int]], iou_thresh: float = 0.45):
    boxes = sorted(boxes, key=lambda b: b[4], reverse=True)
    kept = []
    while boxes:
        best = boxes.pop(0)
        kept.append(best)
        rest = []
        bx1, by1, bx2, by2 = best[0], best[1], best[2], best[3]
        b_area = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        for item in boxes:
            if item[5] != best[5]:
                rest.append(item)
                continue
            ix1 = max(bx1, item[0])
            iy1 = max(by1, item[1])
            ix2 = min(bx2, item[2])
            iy2 = min(by2, item[3])
            inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
            i_area = max(0.0, item[2] - item[0]) * max(0.0, item[3] - item[1])
            union = b_area + i_area - inter
            iou = inter / union if union > 1e-9 else 0.0
            if iou < iou_thresh:
                rest.append(item)
        boxes = rest
    return kept


class LocalFacePetEngine:
    """Lazy-loaded local ONNX inference engine for faces & pets."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._face_app: Any = None
        self._face_model_loaded: str = ""
        self._pet_session: Any = None
        self._pet_model_path: str = ""

    def _ensure_face_app(self, cfg) -> Any:
        model_name = getattr(cfg, "face_model", "buffalo_l")
        if model_name not in FACE_MODELS:
            model_name = "buffalo_l"
        with self._lock:
            if self._face_app is not None and self._face_model_loaded == model_name:
                return self._face_app
            if ort is None or np is None:
                return None
            if not _normalize_insightface_layout(cfg, model_name):
                return None
            try:
                from insightface.app import FaceAnalysis  # type: ignore
            except ImportError:
                return None
            providers = select_ort_providers()
            root = str(models_root(cfg))
            app = FaceAnalysis(
                name=model_name,
                root=root,
                providers=providers,
                allowed_modules=["detection", "recognition"],
            )
            app.prepare(ctx_id=0, det_size=(640, 640), det_thresh=0.55)
            self._face_app = app
            self._face_model_loaded = model_name
            return self._face_app

    def _ensure_pet_session(self, cfg) -> Any:
        if not getattr(cfg, "face_detect_pets", True):
            return None
        pet_path = _find_pet_model_path(cfg)
        if pet_path is None or ort is None or np is None:
            return None
        with self._lock:
            if self._pet_session is not None and self._pet_model_path == str(pet_path):
                return self._pet_session
            providers = select_ort_providers()
            self._pet_session = ort.InferenceSession(str(pet_path), providers=providers)
            self._pet_model_path = str(pet_path)
            return self._pet_session

    def detect(self, image_path: str, cfg) -> list[DetectedEntity]:
        """Run local face + optional pet detection on one image file."""
        if Image is None or np is None:
            return []
        try:
            with Image.open(image_path) as raw:
                img_rgb = ImageOps.exif_transpose(raw).convert("RGB")
        except Exception:
            logger.debug("failed opening image %s for face/pet scan", image_path, exc_info=True)
            return []

        # Guard against huge DSLR originals when falling back without a cached proxy.
        if max(img_rgb.size) > 1600:
            img_rgb.thumbnail((1600, 1600), Image.Resampling.BILINEAR)

        iw, ih = img_rgb.size
        if iw <= 0 or ih <= 0:
            return []
        results: list[DetectedEntity] = []

        # 1. Human faces via InsightFace ONNX.
        face_app = self._ensure_face_app(cfg)
        if face_app is not None:
            bgr = np.asarray(img_rgb, dtype=np.uint8)[:, :, ::-1].copy()
            try:
                faces = face_app.get(bgr)
            except Exception:
                logger.warning("InsightFace inference failed on %s", image_path, exc_info=True)
                faces = []
            for f in faces:
                det_score = float(getattr(f, "det_score", 0.0))
                if det_score < 0.55:
                    continue
                x1, y1, x2, y2 = [float(v) for v in f.bbox[:4]]
                x1 = max(0.0, min(float(iw), x1))
                y1 = max(0.0, min(float(ih), y1))
                x2 = max(x1 + 1.0, min(float(iw), x2))
                y2 = max(y1 + 1.0, min(float(ih), y2))
                bw = (x2 - x1) / iw
                bh = (y2 - y1) / ih
                # Skip tiny background crowd faces (< 2.5% of image width/height).
                if bw < 0.025 and bh < 0.025:
                    continue
                emb = getattr(f, "normed_embedding", None)
                if emb is None:
                    emb = getattr(f, "embedding", None)
                if emb is None:
                    continue
                results.append(
                    DetectedEntity(
                        entity_kind="person",
                        bbox=(x1 / iw, y1 / ih, bw, bh),
                        det_score=det_score,
                        embedding=l2_normalize([float(v) for v in emb]),
                    )
                )

        # 2. Cats & dogs via lightweight YOLOv8n ONNX.
        pet_sess = self._ensure_pet_session(cfg)
        if pet_sess is not None:
            try:
                resized = img_rgb.resize((640, 640), Image.Resampling.BILINEAR)
                arr = np.asarray(resized, dtype=np.float32) / 255.0
                inp = np.transpose(arr, (2, 0, 1))[None, ...]
                input_name = pet_sess.get_inputs()[0].name
                out = pet_sess.run(None, {input_name: inp})[0]
                preds = out[0].T  # (8400, 84) -> [cx, cy, w, h, 80 class scores]
                cat_scores = preds[:, 4 + COCO_CAT_CLASS]
                dog_scores = preds[:, 4 + COCO_DOG_CLASS]
                raw_boxes: list[tuple[float, float, float, float, float, int]] = []
                for idx in np.flatnonzero(np.maximum(cat_scores, dog_scores) >= 0.45):
                    c_sc = float(cat_scores[idx])
                    d_sc = float(dog_scores[idx])
                    cls_id = COCO_CAT_CLASS if c_sc >= d_sc else COCO_DOG_CLASS
                    conf = max(c_sc, d_sc)
                    cx, cy, bw_px, bh_px = [float(v) for v in preds[idx, :4]]
                    x1 = max(0.0, (cx - bw_px * 0.5) / 640.0)
                    y1 = max(0.0, (cy - bh_px * 0.5) / 640.0)
                    x2 = min(1.0, (cx + bw_px * 0.5) / 640.0)
                    y2 = min(1.0, (cy + bh_px * 0.5) / 640.0)
                    if (x2 - x1) < 0.04 or (y2 - y1) < 0.04:
                        continue
                    raw_boxes.append((x1, y1, x2, y2, conf, cls_id))
                for x1, y1, x2, y2, conf, cls_id in _nms_boxes(raw_boxes):
                    bbox = (x1, y1, x2 - x1, y2 - y1)
                    emb = _extract_pet_patch_embedding(img_rgb, bbox)
                    if not emb:
                        continue
                    results.append(
                        DetectedEntity(
                            entity_kind="cat" if cls_id == COCO_CAT_CLASS else "dog",
                            bbox=bbox,
                            det_score=conf,
                            embedding=emb,
                        )
                    )
            except Exception:
                logger.warning("pet ONNX inference failed on %s", image_path, exc_info=True)

        return results


ENGINE = LocalFacePetEngine()


# ---------------------------------------------------------------------------
# Incremental matching & library clustering (iOS-like seamless assignment)
# ---------------------------------------------------------------------------


def recompute_person_centroid(database, person_id: int) -> list[float] | None:
    """Recompute and persist the prototype centroid for one person or pet."""
    faces = database.faces_for_person(person_id, include_pending=False)
    embeddings = [
        decode_embedding(f["embedding"])
        for f in faces
        if f.get("embedding") and f.get("status") in ("manual", "auto")
    ]
    embeddings = [e for e in embeddings if e]
    centroid = compute_centroid(embeddings)
    database.update_person_centroid(person_id, encode_embedding(centroid))
    # Also ensure cover_face_id points to the sharpest face if missing.
    person = database.get_person(person_id)
    if person and faces:
        valid_ids = {f["id"] for f in faces}
        if not person.get("cover_face_id") or person["cover_face_id"] not in valid_ids:
            best = max(faces, key=lambda f: (f.get("det_score") or 0.0, (f.get("bbox_w") or 0.0) * (f.get("bbox_h") or 0.0)))
            database.set_person_flags(person_id, cover_face_id=best["id"])
    return centroid


def match_and_cluster_library(database, cfg) -> dict[str, int]:
    """Match unassigned faces/pets to known profiles, then cluster leftovers.

    Two-stage iOS-inspired logic:
    1. Compare unassigned ``auto`` faces against confirmed people/pets of the
       same ``entity_kind``:
       - distance <= ``threshold`` -> auto-assign (``status='auto'``)
       - distance <= ``threshold + 0.14`` -> queue for user confirmation (``status='pending'``)
    2. Cluster remaining unassigned embeddings per ``entity_kind`` via DBSCAN to
       discover new unnamed people/pets cleanly without touching manual labels.
    """
    model_name = getattr(cfg, "face_model", "buffalo_l")
    threshold = float(getattr(cfg, "face_match_threshold", 0.40))
    pending_cutoff = min(0.75, threshold + 0.14)
    min_cluster = max(2, int(getattr(cfg, "face_min_cluster_size", 5)))

    # 1. Build prototype centroids for every confirmed person/pet (named first).
    all_people = database.all_people_raw()
    prototypes: dict[str, list[tuple[int, list[float], bool]]] = {
        "person": [],
        "cat": [],
        "dog": [],
    }
    for p in all_people:
        centroid = recompute_person_centroid(database, p["id"])
        if centroid:
            kind = p.get("kind") or "person"
            prototypes.setdefault(kind, []).append(
                (p["id"], centroid, bool((p.get("name") or "").strip()))
            )

    auto_matched = 0
    pending_queued = 0
    clusters_created = 0

    for entity_kind in ("person", "cat", "dog"):
        unassigned = database.unassigned_faces(model_name=model_name, entity_kind=entity_kind)
        if not unassigned:
            continue
        # Pets use a slightly wider threshold for visual descriptors.
        eff_thresh = threshold if entity_kind == "person" else min(0.45, threshold * 0.85)
        eff_pending = pending_cutoff if entity_kind == "person" else min(0.60, eff_thresh + 0.12)
        kind_protos = prototypes.get(entity_kind, [])

        leftovers: list[dict[str, Any]] = []
        leftover_vecs: list[list[float]] = []

        for face in unassigned:
            vec = decode_embedding(face.get("embedding"))
            if not vec:
                continue
            best_pid = None
            best_dist = 2.0
            for pid, pvec, _is_named in kind_protos:
                if len(pvec) != len(vec):
                    continue
                d = cosine_distance(vec, pvec)
                if d < best_dist:
                    best_dist = d
                    best_pid = pid
            if best_pid is not None and best_dist <= eff_thresh:
                database.set_face_assignment(face["id"], best_pid, status="auto")
                auto_matched += 1
            elif best_pid is not None and best_dist <= eff_pending:
                database.set_face_assignment(face["id"], best_pid, status="pending")
                pending_queued += 1
            else:
                leftovers.append(face)
                leftover_vecs.append(vec)

        # 2. Cluster leftovers via DBSCAN (strict eps to avoid false merges).
        if len(leftovers) >= min_cluster:
            cluster_eps = max(0.32, eff_thresh - 0.04)
            labels = cluster_embeddings_dbscan(
                leftover_vecs, eps=cluster_eps, min_samples=min_cluster
            )
            grouped: dict[int, list[int]] = {}
            for idx, lbl in enumerate(labels):
                if lbl >= 0:
                    grouped.setdefault(lbl, []).append(idx)
            for member_indices in grouped.values():
                member_faces = [leftovers[i] for i in member_indices]
                member_vecs = [leftover_vecs[i] for i in member_indices]
                best_cover = max(
                    member_faces,
                    key=lambda f: (
                        f.get("det_score") or 0.0,
                        (f.get("bbox_w") or 0.0) * (f.get("bbox_h") or 0.0),
                    ),
                )
                centroid = compute_centroid(member_vecs)
                new_pid = database.create_person(
                    name="",
                    kind=entity_kind,
                    cover_face_id=best_cover["id"],
                    centroid=encode_embedding(centroid),
                )
                for mf in member_faces:
                    database.set_face_assignment(mf["id"], new_pid, status="auto")
                clusters_created += 1

    # Refresh centroids after batch updates.
    for p in database.all_people_raw():
        recompute_person_centroid(database, p["id"])

    return {
        "auto_matched": auto_matched,
        "pending_queued": pending_queued,
        "clusters_created": clusters_created,
    }


# ---------------------------------------------------------------------------
# Background scan job coordinator
# ---------------------------------------------------------------------------


@dataclass
class PeopleScanJob:
    status: str = "idle"  # idle | running | completed | cancelled | error
    total: int = 0
    done: int = 0
    faces_found: int = 0
    pets_found: int = 0
    cluster_version: int = 0
    message: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict[str, Any]:
        now = time.monotonic()
        if self.started_at:
            end = self.finished_at or now
            elapsed = max(0.0, end - self.started_at)
        else:
            elapsed = 0.0
        return {
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "faces_found": self.faces_found,
            "pets_found": self.pets_found,
            "cluster_version": self.cluster_version,
            "message": self.message,
            "error": self.error,
            "elapsed": round(elapsed, 2),
        }


class PeopleScanCoordinator:
    """Single-slot background worker for incremental face & pet scanning."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job = PeopleScanJob()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._job.to_dict()

    def cancel(self) -> bool:
        with self._lock:
            if self._job.status == "running":
                self._job.cancel_event.set()
                return True
            return False

    def start(
        self,
        database,
        cfg,
        folder: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if self._job.status == "running":
                return self._job.to_dict()
            job = PeopleScanJob(
                status="running",
                message="准备识别人物与宠物…",
                started_at=time.monotonic(),
            )
            self._job = job

        def _worker():
            try:
                model_name = getattr(cfg, "face_model", "buffalo_l")
                photos = database.photos_needing_face_scan(
                    folder=folder, model_name=model_name, force=force
                )
                job.total = len(photos)
                last_cluster_at = time.monotonic()
                photos_since_cluster = 0
                entities_since_cluster = 0

                for idx, photo in enumerate(photos, start=1):
                    if job.cancel_event.is_set():
                        if entities_since_cluster > 0:
                            match_and_cluster_library(database, cfg)
                            job.cluster_version += 1
                        job.status = "cancelled"
                        job.message = "已取消人物识别"
                        return
                    img_path = photo.get("proxy_path") or photo.get("path")
                    if not img_path or not os.path.isfile(img_path):
                        database.mark_photo_faces_scanned(photo["id"], model_name)
                        job.done = idx
                        continue
                    entities = ENGINE.detect(img_path, cfg)
                    records = []
                    for ent in entities:
                        if ent.entity_kind == "person":
                            job.faces_found += 1
                        else:
                            job.pets_found += 1
                        records.append(
                            {
                                "entity_kind": ent.entity_kind,
                                "bbox_x": ent.bbox[0],
                                "bbox_y": ent.bbox[1],
                                "bbox_w": ent.bbox[2],
                                "bbox_h": ent.bbox[3],
                                "det_score": ent.det_score,
                                "embedding": encode_embedding(ent.embedding),
                                "model_name": model_name,
                            }
                        )
                    inserted_ids = database.replace_auto_faces_for_photo(
                        photo["id"], records, model_name
                    )
                    # Generate cropped square avatars for newly detected faces/pets.
                    for fid, rec in zip(inserted_ids, records):
                        photo_folder = (
                            photo.get("folder")
                            or os.path.dirname(photo.get("path") or "")
                        )
                        thumb_file = face_thumbs_dir(cfg, photo_folder) / f"face_{fid}.jpg"
                        saved = crop_entity_thumbnail(
                            img_path,
                            (rec["bbox_x"], rec["bbox_y"], rec["bbox_w"], rec["bbox_h"]),
                            thumb_file,
                        )
                        if saved:
                            database.update_face_thumb(fid, saved)
                    database.mark_photo_faces_scanned(photo["id"], model_name)
                    job.done = idx
                    photos_since_cluster += 1
                    entities_since_cluster += len(records)
                    job.message = f"已扫描 {idx} / {job.total} 张照片…"

                    now = time.monotonic()
                    if entities_since_cluster > 0 and (
                        idx == LIVE_CLUSTER_FIRST_PHOTO
                        or photos_since_cluster >= LIVE_CLUSTER_BATCH_PHOTOS
                        or (now - last_cluster_at) >= LIVE_CLUSTER_INTERVAL_SEC
                    ):
                        match_and_cluster_library(database, cfg)
                        job.cluster_version += 1
                        photos_since_cluster = 0
                        entities_since_cluster = 0
                        last_cluster_at = time.monotonic()

                job.message = "正在自动归类人物与宠物…"
                summary = match_and_cluster_library(database, cfg)
                job.cluster_version += 1
                job.status = "completed"
                job.message = (
                    f"识别完成：共扫描 {job.total} 张，"
                    f"新归入 {summary['auto_matched']} 张，"
                    f"发现 {summary['clusters_created']} 组新面孔"
                )
            except Exception as exc:
                logger.exception("people/pet background scan failed")
                job.status = "error"
                job.error = str(exc)
                job.message = f"识别出错：{exc}"
            finally:
                if not job.finished_at:
                    job.finished_at = time.monotonic()

        t = threading.Thread(target=_worker, name="lumina-people-scan", daemon=True)
        t.start()
        return job.to_dict()


SCAN_COORDINATOR = PeopleScanCoordinator()
