"""Local multimodal vector search engine (ONNX CLIP + zero-dep visual descriptor).

Reuses the local ONNX Runtime hardware acceleration (CoreML / CUDA / DirectML / CPU)
from :mod:`photo_reviewer.faces`. When a local ONNX CLIP/SigLIP vision+text model is
installed under ``<models_dir>/clip/`` (``vision.onnx``, ``text.onnx``, ``vocab.txt``),
natural-language vector search runs 100% offline across the entire library.

Even before an ONNX CLIP model is downloaded, a lightweight 64-dim L2-normalized
visual descriptor (HSV color histogram + spatial layout + edge energy) powers
instant image-to-image search ("找相似画面") and vector-assisted duplicate detection.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .faces import (
    active_accelerator_label,
    decode_embedding,
    encode_embedding,
    l2_normalize,
    models_root,
    select_ort_providers,
)

logger = logging.getLogger(__name__)

VISUAL_FALLBACK_MODEL = "visual-hist-v1"
DEFAULT_CLIP_MODEL = "cn-clip-vit-b16"


def clip_model_dir(cfg) -> Path:
    """Directory holding optional ONNX CLIP/SigLIP vision & text encoders."""
    return models_root(cfg) / "clip"


def has_clip_onnx_model(cfg) -> bool:
    """True when both ``vision.onnx`` and ``text.onnx`` are present locally."""
    root = clip_model_dir(cfg)
    return (root / "vision.onnx").is_file() and (root / "text.onnx").is_file()


def active_vector_model(cfg) -> str:
    """Return the active embedding model key stored in ``photo_embeddings``."""
    if has_clip_onnx_model(cfg):
        return (getattr(cfg, "vector_model", None) or DEFAULT_CLIP_MODEL).strip() or DEFAULT_CLIP_MODEL
    return VISUAL_FALLBACK_MODEL


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in ``[-1, 1]`` for two L2-normalized vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    for x, y in zip(a, b):
        dot += x * y
    return max(-1.0, min(1.0, float(dot)))


def compute_visual_descriptor(image_path: str) -> list[float] | None:
    """Compute a 64-dim L2-normalized visual descriptor using Pillow only.

    Layout (64 floats):
    - 16-bin Hue histogram (weighted by saturation so greys don't pollute red)
    - 8-bin Saturation histogram
    - 8-bin Value (brightness) histogram
    - 4x4 spatial luminance grid (16 floats)
    - 4x4 spatial warm-vs-cool chroma grid (16 floats)
    """
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(image_path) as raw:
            img = ImageOps.exif_transpose(raw).convert("RGB")
            img = img.resize((64, 64), Image.Resampling.BILINEAR)
            rgb_pixels = list(img.getdata())
            hsv_pixels = list(img.convert("HSV").getdata())
    except Exception:
        logger.debug("visual descriptor failed for %s", image_path, exc_info=True)
        return None

    n = max(1, len(hsv_pixels))
    h_bins = [0.0] * 16
    s_bins = [0.0] * 8
    v_bins = [0.0] * 8
    for h, s, v in hsv_pixels:
        weight = 0.15 + (s / 255.0)
        h_bins[min(15, (h * 16) // 256)] += weight
        s_bins[min(7, (s * 8) // 256)] += 1.0
        v_bins[min(7, (v * 8) // 256)] += 1.0

    h_sum = sum(h_bins) or 1.0
    h_bins = [x / h_sum for x in h_bins]
    s_bins = [x / n for x in s_bins]
    v_bins = [x / n for x in v_bins]

    luma_grid = [0.0] * 16
    warm_grid = [0.0] * 16
    cell_counts = [0] * 16
    for idx, (r, g, b) in enumerate(rgb_pixels):
        y = idx // 64
        x = idx % 64
        cell = (y // 16) * 4 + (x // 16)
        luma = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        warm = ((r - b) / 255.0 + 1.0) * 0.5
        luma_grid[cell] += luma
        warm_grid[cell] += warm
        cell_counts[cell] += 1

    for c in range(16):
        cnt = max(1, cell_counts[c])
        luma_grid[c] = (luma_grid[c] / cnt) * 0.5
        warm_grid[c] = (warm_grid[c] / cnt) * 0.5

    vec = h_bins + s_bins + v_bins + luma_grid + warm_grid
    return l2_normalize(vec)


class _ClipOnnxRuntime:
    """Lazy-loaded ONNX Runtime session pair for CLIP/SigLIP vision & text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dir_key: str | None = None
        self._vision = None
        self._text = None
        self._vocab: dict[str, int] | None = None

    def _ensure_loaded(self, cfg) -> bool:
        if not has_clip_onnx_model(cfg):
            return False
        root = clip_model_dir(cfg)
        key = str(root.resolve())
        with self._lock:
            if self._dir_key == key and self._vision is not None and self._text is not None:
                return True
            try:
                import onnxruntime as ort
            except ImportError:
                return False
            providers = select_ort_providers()
            try:
                self._vision = ort.InferenceSession(str(root / "vision.onnx"), providers=providers)
                self._text = ort.InferenceSession(str(root / "text.onnx"), providers=providers)
                vocab_path = root / "vocab.txt"
                vocab: dict[str, int] = {}
                if vocab_path.is_file():
                    for idx, line in enumerate(vocab_path.read_text(encoding="utf-8").splitlines()):
                        tok = line.strip()
                        if tok:
                            vocab[tok] = idx
                self._vocab = vocab
                self._dir_key = key
                return True
            except Exception:
                logger.warning("Failed to load CLIP ONNX sessions from %s", root, exc_info=True)
                self._vision = None
                self._text = None
                return False

    def encode_image(self, image_path: str, cfg) -> list[float] | None:
        if not self._ensure_loaded(cfg):
            return None
        try:
            import numpy as np
            from PIL import Image, ImageOps
        except ImportError:
            return None
        try:
            with Image.open(image_path) as raw:
                img = ImageOps.exif_transpose(raw).convert("RGB").resize((224, 224), Image.Resampling.BICUBIC)
            arr = np.asarray(img, dtype=np.float32) / 255.0
            mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
            std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
            arr = (arr - mean) / std
            tensor = np.transpose(arr, (2, 0, 1))[None, ...]
            input_name = self._vision.get_inputs()[0].name
            out = self._vision.run(None, {input_name: tensor})[0]
            flat = [float(x) for x in out.reshape(-1)]
            return l2_normalize(flat)
        except Exception:
            logger.debug("CLIP vision encode failed for %s", image_path, exc_info=True)
            return None

    def encode_text(self, query: str, cfg) -> list[float] | None:
        if not self._ensure_loaded(cfg):
            return None
        try:
            import numpy as np
        except ImportError:
            return None
        vocab = self._vocab or {}
        cls_id = vocab.get("[CLS]", 101)
        sep_id = vocab.get("[SEP]", 102)
        unk_id = vocab.get("[UNK]", 100)
        tokens = [cls_id]
        for ch in (query or "").strip().lower():
            if ch.isspace():
                continue
            tokens.append(vocab.get(ch, unk_id))
            if len(tokens) >= 51:
                break
        tokens.append(sep_id)
        max_len = 52
        while len(tokens) < max_len:
            tokens.append(0)
        try:
            inp = np.array([tokens[:max_len]], dtype=np.int64)
            input_name = self._text.get_inputs()[0].name
            out = self._text.run(None, {input_name: inp})[0]
            flat = [float(x) for x in out.reshape(-1)]
            return l2_normalize(flat)
        except Exception:
            logger.debug("CLIP text encode failed for %r", query, exc_info=True)
            return None


_CLIP_RUNTIME = _ClipOnnxRuntime()


def _best_image_path(photo: dict[str, Any]) -> str | None:
    """Prefer proxy/thumbnail for fast vector extraction on large DSLR photos."""
    for key in ("thumb_path", "proxy_path", "path", "original_path"):
        candidate = photo.get(key)
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def compute_photo_embedding(photo: dict[str, Any], cfg, model: str | None = None) -> tuple[str, list[float]] | None:
    """Extract an embedding for ``photo`` under ``model`` (or active model)."""
    target_model = model or active_vector_model(cfg)
    img_path = _best_image_path(photo)
    if not img_path:
        return None
    if target_model != VISUAL_FALLBACK_MODEL and has_clip_onnx_model(cfg):
        vec = _CLIP_RUNTIME.encode_image(img_path, cfg)
        if vec:
            return target_model, vec
    vec = compute_visual_descriptor(img_path)
    if vec:
        return VISUAL_FALLBACK_MODEL, vec
    return None


def ensure_photo_embedding(database, cfg, photo: dict[str, Any], model: str | None = None) -> tuple[str, list[float]] | None:
    """Load cached embedding from DB, computing and persisting on a cache miss."""
    target_model = model or active_vector_model(cfg)
    existing = database.get_photo_embedding(photo["id"], target_model)
    if existing:
        vec = decode_embedding(existing)
        if vec:
            return target_model, vec
    computed = compute_photo_embedding(photo, cfg, model=target_model)
    if not computed:
        return None
    used_model, vec = computed
    blob = encode_embedding(vec)
    if blob:
        database.upsert_photo_embedding(photo["id"], used_model, len(vec), blob)
    return used_model, vec


def vector_engine_status(database, cfg, folder: str | None = None) -> dict[str, Any]:
    """Return vector search engine readiness & index coverage stats."""
    clip_ready = has_clip_onnx_model(cfg)
    model = active_vector_model(cfg)
    indexed = database.count_photo_embeddings(model, folder=folder)
    total = database.stats(folder).get("total", 0)
    return {
        "enabled": bool(getattr(cfg, "vector_search_enabled", True)),
        "clip_ready": clip_ready,
        "active_model": model,
        "clip_model": getattr(cfg, "vector_model", DEFAULT_CLIP_MODEL),
        "accelerator": active_accelerator_label(),
        "indexed_count": indexed,
        "total_photos": total,
        "models_dir": str(clip_model_dir(cfg)),
    }


def vector_text_search(
    database,
    cfg,
    query: str,
    candidates: list[dict[str, Any]],
    top_k: int = 200,
    min_similarity: float | None = None,
) -> dict[str, Any]:
    """Natural-language vector search over ``candidates`` using local CLIP ONNX."""
    if not has_clip_onnx_model(cfg):
        return {
            "clip_ready": False,
            "model": VISUAL_FALLBACK_MODEL,
            "photos": [],
            "scores": {},
            "warning": (
                f"本地向量模型尚未安装（请将 vision.onnx、text.onnx 与 vocab.txt 放入 "
                f"{clip_model_dir(cfg)}），已回退关键词匹配。"
            ),
        }
    q_vec = _CLIP_RUNTIME.encode_text(query, cfg)
    if not q_vec:
        return {
            "clip_ready": True,
            "model": active_vector_model(cfg),
            "photos": [],
            "scores": {},
            "warning": "向量文本编码失败，请检查模型文件是否完整。",
        }
    model = active_vector_model(cfg)
    threshold = (
        float(min_similarity)
        if min_similarity is not None
        else float(getattr(cfg, "vector_min_similarity", 0.22))
    )
    candidate_ids = [p["id"] for p in candidates]
    emb_map = database.list_photo_embeddings(model, photo_ids=candidate_ids)
    scored: list[tuple[float, dict[str, Any]]] = []
    for photo in candidates:
        blob = emb_map.get(photo["id"])
        if not blob:
            continue
        vec = decode_embedding(blob)
        if not vec:
            continue
        sim = cosine_similarity(q_vec, vec)
        if sim >= threshold:
            scored.append((sim, photo))
    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[: max(1, int(top_k))]
    return {
        "clip_ready": True,
        "model": model,
        "photos": [p for _s, p in top],
        "scores": {p["id"]: round(s, 4) for s, p in top},
        "indexed": len(emb_map),
        "warning": (
            "当前相册尚未建立向量索引，请在「设置 → 模型」中点击「建立向量索引」。"
            if not emb_map
            else None
        ),
    }


def vector_image_search(
    database,
    cfg,
    target_photo: dict[str, Any],
    candidates: list[dict[str, Any]],
    top_k: int = 48,
    min_similarity: float = 0.68,
) -> dict[str, Any]:
    """Find photos visually similar to ``target_photo`` (image-to-image search)."""
    res = ensure_photo_embedding(database, cfg, target_photo)
    if not res:
        return {"model": active_vector_model(cfg), "photos": [], "scores": {}}
    model, target_vec = res
    candidate_ids = [p["id"] for p in candidates if p["id"] != target_photo["id"]]
    emb_map = database.list_photo_embeddings(model, photo_ids=candidate_ids)

    # Lazily backfill missing visual descriptors for small/medium sets so
    # image-to-image search works immediately even before a manual index run.
    scored: list[tuple[float, dict[str, Any]]] = []
    backfilled = 0
    for photo in candidates:
        pid = photo["id"]
        if pid == target_photo["id"]:
            continue
        blob = emb_map.get(pid)
        vec = decode_embedding(blob) if blob else None
        if vec is None and backfilled < 240:
            computed = ensure_photo_embedding(database, cfg, photo, model=model)
            if computed and computed[0] == model:
                vec = computed[1]
                backfilled += 1
        if not vec:
            continue
        sim = cosine_similarity(target_vec, vec)
        if sim >= min_similarity:
            scored.append((sim, photo))
    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[: max(1, int(top_k))]
    return {
        "model": model,
        "photos": [p for _s, p in top],
        "scores": {p["id"]: round(s, 4) for s, p in top},
    }


@dataclass
class VectorIndexJob:
    status: str = "idle"  # idle | running | completed | cancelled | error
    model: str = VISUAL_FALLBACK_MODEL
    total: int = 0
    done: int = 0
    embedded: int = 0
    skipped: int = 0
    message: str = ""
    error: str = ""
    started_at: float = 0.0
    cancelled: bool = False
    folder: str | None = None

    def to_dict(self) -> dict[str, Any]:
        elapsed = round(max(0.0, time.time() - self.started_at), 2) if self.started_at else 0.0
        return {
            "status": self.status,
            "model": self.model,
            "total": self.total,
            "done": self.done,
            "embedded": self.embedded,
            "skipped": self.skipped,
            "message": self.message,
            "error": self.error,
            "elapsed": elapsed,
            "cancelled": self.cancelled,
        }


class VectorIndexManager:
    """Single background worker for incremental photo embedding extraction."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job = VectorIndexJob()
        self._thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._job.to_dict()

    def cancel(self) -> bool:
        with self._lock:
            if self._job.status != "running":
                return False
            self._job.cancelled = True
            self._job.message = "正在取消向量索引…"
            return True

    def start(self, database, cfg, folder: str | None = None, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._job.status == "running":
                return self._job.to_dict()
            model = active_vector_model(cfg)
            self._job = VectorIndexJob(
                status="running",
                model=model,
                started_at=time.time(),
                folder=folder,
                message="正在准备向量索引…",
            )
            job = self._job

        def _worker() -> None:
            try:
                photos = database.list_photos(
                    folder=folder,
                    exclude_deleted=True,
                    sort="time_desc",
                    limit=100000,
                )
                existing = (
                    {}
                    if force
                    else database.list_photo_embeddings(model, photo_ids=[p["id"] for p in photos])
                )
                pending = [p for p in photos if p["id"] not in existing]
                with self._lock:
                    job.total = len(pending)
                    job.skipped = len(photos) - len(pending)
                    job.message = (
                        f"正在提取 {len(pending)} 张照片的视觉向量…"
                        if pending
                        else "所有照片均已完成向量索引"
                    )
                for photo in pending:
                    if job.cancelled:
                        with self._lock:
                            job.status = "cancelled"
                            job.message = f"已取消（已完成 {job.embedded}/{job.total}）"
                        return
                    res = compute_photo_embedding(photo, cfg, model=model)
                    if res:
                        used_model, vec = res
                        blob = encode_embedding(vec)
                        if blob:
                            database.upsert_photo_embedding(photo["id"], used_model, len(vec), blob)
                            with self._lock:
                                job.embedded += 1
                    with self._lock:
                        job.done += 1
                        job.message = f"正在提取向量 {job.done}/{job.total}…"
                with self._lock:
                    job.status = "completed"
                    job.message = f"向量索引完成：新增 {job.embedded} 张，跳过 {job.skipped} 张"
            except Exception as exc:
                logger.warning("Vector index worker failed", exc_info=True)
                with self._lock:
                    job.status = "error"
                    job.error = str(exc)
                    job.message = f"向量索引失败：{exc}"

        self._thread = threading.Thread(target=_worker, name="lumina-vector-index", daemon=True)
        self._thread.start()
        return self.status()


VECTOR_INDEX_MANAGER = VectorIndexManager()
