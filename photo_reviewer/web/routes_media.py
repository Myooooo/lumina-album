"""Thumbnails, proxies, originals, video and Live Photo.

Registered by :func:`photo_reviewer.server.create_app`; the route bodies are
grouped by the part of the app they serve.
"""

from __future__ import annotations

import os
import logging
from typing import Any

from flask import jsonify, send_file

from .. import video

from ..cache import ensure_cache_dirs

from ..scanner import (
    start_video_job,
    video_job_for,
)

from ..thumbnailer import make_proxy

from .constants import (
    _IMMUTABLE_IMAGE_MAX_AGE,
)

from .helpers import (
    _image_cache_timeout,
    _thumbnail_path,
)

from pathlib import Path

logger = logging.getLogger(__name__)


def register(app, cfg, database):
    """Register these routes on ``app``."""

    @app.get("/api/thumbnail/<int:photo_id>")
    def api_thumbnail(photo_id: int):
        """Serve the small gallery thumbnail.

        Lookup order is ``thumb_path`` → sibling of ``proxy_path`` → generate.
        Only the last branch decodes the picture, so a cached thumbnail is
        served straight from disk and a raw file never needs the sensor data.

        ``?v=<hash>`` marks the URL as content-addressed: the client sends the
        row's ``thumb_version``, which changes whenever the file is re-rendered.
        Those responses are cached for a year, so scrolling a long album back
        and forth costs no requests at all. Without the parameter (an old
        bookmark, a manual visit) the response stays revalidated, because the
        file behind a bare id can change under it.
        """
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        cache_timeout = _image_cache_timeout()
        thumb = _thumbnail_path(photo)
        if thumb:
            return send_file(
                thumb,
                mimetype="image/jpeg",
                conditional=True,
                max_age=cache_timeout,
            )
        path = photo.get("path") or photo.get("original_path")
        if path and os.path.exists(path):
            try:
                folder = photo.get("folder") or os.path.dirname(path)
                thumb_dir = ensure_cache_dirs(folder, cfg.cache_dir_name)
                proxy_path, _, _, phash, thumb_path = make_proxy(
                    path,
                    max_edge=cfg.proxy_max_edge,
                    cache_dir=str(thumb_dir),
                    quality=cfg.proxy_quality,
                    thumb_size=cfg.gallery_thumb_size,
                    thumb_quality=cfg.thumb_quality,
                )
                database.update_cache_paths(photo_id, thumb_path, proxy_path, phash)
                return send_file(
                    thumb_path,
                    mimetype="image/jpeg",
                    conditional=True,
                    max_age=cache_timeout,
                )
            except (OSError, ValueError):
                logger.warning(
                    "thumbnail generation failed for photo %s", photo_id, exc_info=True
                )
                return jsonify({"error": "无法生成缩略图"}), 500
        return jsonify({"error": "缩略图不存在"}), 404

    @app.get("/api/proxy/<int:photo_id>")
    def api_proxy(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        proxy = photo.get("proxy_path")
        if not proxy or not os.path.exists(proxy):
            path = photo.get("path") or photo.get("original_path")
            if not path or not os.path.exists(path):
                return jsonify({"error": "代理图不存在"}), 404
            try:
                folder = photo.get("folder") or os.path.dirname(path)
                proxy_dir = ensure_cache_dirs(folder, cfg.cache_dir_name)
                proxy, _, _, _, _ = make_proxy(
                    path,
                    max_edge=cfg.proxy_max_edge,
                    cache_dir=str(proxy_dir),
                    quality=cfg.proxy_quality,
                    thumb_size=cfg.gallery_thumb_size,
                    thumb_quality=cfg.thumb_quality,
                )
                database.update_proxy_path(photo_id, proxy)
            except (OSError, ValueError):
                logger.warning(
                    "proxy generation failed for photo %s", photo_id, exc_info=True
                )
                return jsonify({"error": "无法生成代理图"}), 500
        return send_file(proxy, mimetype="image/jpeg", conditional=True)

    @app.get("/api/original/<int:photo_id>")
    def api_original(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        path = photo.get("path") or photo.get("original_path")
        if not path or not os.path.exists(path):
            return jsonify({"error": "原图不存在"}), 404
        return send_file(path, conditional=True)

    # ------------------------------------------------------------------
    # Video
    #
    # Three URLs, one for each thing the UI shows: the five-second preview the
    # detail dialog loops, the original file played full screen, and a
    # browser-safe copy for the containers a browser refuses. Range requests are
    # handled by ``send_file`` itself, which is what makes seeking work without
    # any hand-written byte-range code.
    # ------------------------------------------------------------------

    @app.post("/api/video/install")
    def api_video_install():
        try:
            job = video.FFMPEG_INSTALLER.start(cfg, database)
            return jsonify({"ok": True, "job": job})
        except Exception as exc:
            return jsonify({"error": f"启动 ffmpeg 安装失败：{exc}"}), 500

    @app.get("/api/video/install/status")
    def api_video_install_status():
        return jsonify({"job": video.FFMPEG_INSTALLER.status()})

    @app.post("/api/video/install/cancel")
    def api_video_install_cancel():
        return jsonify(
            {
                "cancelled": video.FFMPEG_INSTALLER.cancel(),
                "job": video.FFMPEG_INSTALLER.status(),
            }
        )

    @app.get("/api/video/status")
    def api_video_status():
        """Whether the video pipeline can run on this machine.

        The settings dialog shows this: ffmpeg is a system dependency, so the
        user has to be told "not found" somewhere visible rather than discovering
        it when a card has no picture.
        """
        available = video.find_tools(cfg.ffmpeg_path or None, refresh=True) is not None
        return jsonify(
            {
                "available": available,
                "version": video.tools_version(cfg.ffmpeg_path or None) if available else "",
                "extensions": list(cfg.video_extensions),
            }
        )

    def _video_row(photo_id: int):
        photo = database.get_photo(photo_id)
        if not photo or (photo.get("media_kind") or "photo") != "video":
            return None
        return photo

    def _video_cache_dir(photo: dict[str, Any]) -> str:
        folder = photo.get("folder") or os.path.dirname(photo["path"])
        return str(ensure_cache_dirs(folder, cfg.cache_dir_name))

    def _cache_key(photo: dict[str, Any]) -> str:
        return video.video_cache_key(photo["path"])

    def _stream_response(path: str, mimetype: str):
        """Send a media file with the headers a ``<video>`` element expects."""
        response = send_file(
            path,
            mimetype=mimetype,
            conditional=True,
            max_age=_IMMUTABLE_IMAGE_MAX_AGE,
        )
        # Werkzeug answers Range requests but does not advertise it on every
        # response; players use the header to decide whether seeking is possible.
        response.headers["Accept-Ranges"] = "bytes"
        return response

    def _guess_mimetype(path: str) -> str:
        ext = Path(path).suffix.lower()
        return {
            ".mp4": "video/mp4",
            ".m4v": "video/mp4",
            ".mov": "video/quicktime",
            ".qt": "video/quicktime",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
            ".avi": "video/x-msvideo",
            ".ts": "video/mp2t",
            ".mts": "video/mp2t",
            ".m2ts": "video/mp2t",
            ".3gp": "video/3gpp",
            ".ogv": "video/ogg",
            ".wmv": "video/x-ms-wmv",
        }.get(ext, "video/mp4")

    @app.get("/api/video/<int:photo_id>/clip")
    def api_video_clip(photo_id: int):
        """The short preview the detail dialog loops.

        Generated on first open (~200 ms) and cached next to the poster. The
        dialog paints the poster immediately and cross-fades to this once it can
        play, so the delay is invisible.
        """
        photo = _video_row(photo_id)
        if not photo:
            return jsonify({"error": "视频不存在"}), 404
        clip_path = photo.get("clip_path")
        if not clip_path or not os.path.exists(clip_path):
            if not video.tools_available(cfg.ffmpeg_path or None):
                return jsonify({"error": "未安装 ffmpeg，无法生成预览"}), 503
            cache_dir = _video_cache_dir(photo)
            clip_path = os.path.join(cache_dir, f"{_cache_key(photo)}_clip.mp4")
            info = video.probe(photo["path"], cfg.ffmpeg_path or None)
            if not video.make_clip(
                photo["path"],
                clip_path,
                info,
                seconds=cfg.video_clip_seconds,
                max_edge=cfg.video_clip_max_edge,
                crf=cfg.video_clip_crf,
                include_audio=cfg.video_clip_audio,
                override_dir=cfg.ffmpeg_path or None,
            ):
                return jsonify({"error": "无法生成视频预览"}), 500
            database.set_video_paths(photo_id, clip_path=clip_path)
        return _stream_response(clip_path, "video/mp4")

    @app.get("/api/video/<int:photo_id>/stream")
    def api_video_stream(photo_id: int):
        """The original file, byte ranges included.

        This is what the full-screen player uses: no transcoding, full quality,
        and the browser's own seek handling.
        """
        photo = _video_row(photo_id)
        if not photo:
            return jsonify({"error": "视频不存在"}), 404
        path = photo.get("path")
        if not path or not os.path.exists(path):
            return jsonify({"error": "视频文件不存在"}), 404
        return _stream_response(path, _guess_mimetype(path))

    @app.get("/api/video/<int:photo_id>/playable")
    def api_video_playable(photo_id: int):
        """A copy every desktop browser can decode.

        Tried only after the original failed to play, which is the client's cue
        that the codec or the container is the problem. A stream copy is enough
        for the common case (MKV/MTS carrying H.264); a genuine transcode runs
        as a tracked job so the toolbar can show its progress.

        Returns 200 with the file when it is ready, 202 with a job id while it is
        being prepared, and 503 when ffmpeg is missing.
        """
        photo = _video_row(photo_id)
        if not photo:
            return jsonify({"error": "视频不存在"}), 404
        play_path = photo.get("play_path")
        if play_path and os.path.exists(play_path):
            return _stream_response(play_path, "video/mp4")
        if not video.tools_available(cfg.ffmpeg_path or None):
            return jsonify({"error": "未安装 ffmpeg，无法转换此视频"}), 503
        running = video_job_for(photo_id)
        if running:
            return jsonify({"job_id": running.id, "state": "running"}), 202
        job = start_video_job(photo_id, database, cfg)
        return jsonify({"job_id": job.id, "state": job.status}), 202

    @app.get("/api/photo/<int:photo_id>/live")
    def api_live_video(photo_id: int):
        """The moving part of a motion photo or Live Photo."""
        photo = database.get_photo(photo_id)
        if not photo:
            return jsonify({"error": "照片不存在"}), 404
        live_path = photo.get("live_path")
        if not live_path or not os.path.exists(live_path):
            return jsonify({"error": "这张照片没有实况视频"}), 404
        return _stream_response(live_path, _guess_mimetype(live_path))
