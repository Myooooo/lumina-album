"""Settings, UI state and prompt presets.

Registered by :func:`photo_reviewer.server.create_app`; the route bodies are
grouped by the part of the app they serve.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from flask import jsonify, request

from ..api_client import (
    ANTI_AI_MARKER,
    JSON_FORMAT_MARKER,
    LEGACY_JSON_FORMAT_MARKER,
    PROMPT_PREAMBLE,
    PROMPT_PRESETS,
    strip_fixed_sections,
)

from ..config import (
    EDITABLE_FIELDS,
    FOOTPRINT_VIEW_MODES,
    MAP_TILE_PROVIDERS,
    PREVIEW_MODES,
    VIDEO_PROCESSING,
)

from ..geocode import (
    PROVIDERS as GEOCODING_PROVIDERS,
    reverse_geocode,
)

from ..paths import canonical_path

from .constants import (
    UI_STATE_HISTORY_LIMIT,
)

from .helpers import (
    MAX_PROMPT_CHARS,
    MAX_CUSTOM_PRESETS,
    _save_settings_to_db,
    custom_prompt_presets,
    hidden_folder_list,
    save_custom_prompt_presets,
)

logger = logging.getLogger(__name__)


def register(app, cfg, database):
    """Register these routes on ``app``."""

    @app.get("/api/config")
    def api_config():
        return jsonify(cfg.to_dict())

    @app.get("/api/ui-state")
    def api_ui_state():
        """Return the browser-independent UI state.

        The folder history, the open album and the remembered card path used to
        live in ``localStorage``, so clearing the browser's site data threw the
        album's memory away. They are kept in the database instead, next to the
        photos they describe.

        Hidden albums are stripped here as well as in ``/api/folders``: the
        history is a path selector too, and a refresh must never open one.
        """
        state = database.get_ui_state()
        hidden = {
            os.path.normcase(canonical_path(item))
            for item in hidden_folder_list(database)
        }
        history = state.get("folder_history")
        if isinstance(history, list):
            state["folder_history"] = [
                item
                for item in history
                if isinstance(item, str)
                and os.path.normcase(canonical_path(item)) not in hidden
            ]
        remembered = state.get("folder")
        if isinstance(remembered, str) and os.path.normcase(
            canonical_path(remembered)
        ) in hidden:
            state["folder"] = ""
        return jsonify({"state": state})

    @app.post("/api/ui-state")
    def api_save_ui_state():
        """Merge a partial UI state into the stored one.

        Only known, bounded keys are accepted: this endpoint is writable from the
        browser, so it must not become a way to stuff arbitrary rows into the
        database. Values are validated into a staging dict first, so a rejected
        field cannot leave half an update behind.
        """
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "请求内容不合法"}), 400
        staged: dict[str, Any] = {}
        hidden = {
            os.path.normcase(canonical_path(item))
            for item in hidden_folder_list(database)
        }

        if "folder" in data:
            value = str(data["folder"] or "").strip()[:512]
            # A hidden album is never the remembered one: the next refresh has
            # to land back on the last visible album.
            if value and os.path.normcase(canonical_path(value)) in hidden:
                value = ""
            staged["folder"] = value

        if "folder_history" in data:
            raw = data["folder_history"]
            if not isinstance(raw, list):
                return jsonify({"error": "folder_history 必须是数组"}), 400
            history = []
            seen = set()
            for item in raw:
                text = str(item or "").strip()[:512]
                if not text:
                    continue
                if os.path.normcase(canonical_path(text)) in hidden:
                    continue
                key = os.path.normcase(text.rstrip("\\/"))
                if key in seen:
                    continue
                seen.add(key)
                history.append(text)
                if len(history) >= UI_STATE_HISTORY_LIMIT:
                    break
            staged["folder_history"] = history

        if "import_source" in data:
            staged["import_source"] = str(data["import_source"] or "").strip()[:512]

        # The duplicate sheet's similarity threshold. Remembered because it is a
        # judgement the reader has already made once - finding 6 again after
        # settling on 4 would mean re-deciding every time the page is opened.
        if "dup_threshold" in data:
            try:
                value = int(data["dup_threshold"])
            except (TypeError, ValueError):
                return jsonify({"error": "dup_threshold 必须是整数"}), 400
            staged["dup_threshold"] = max(0, min(value, 24))

        if "theme" in data:
            theme = str(data["theme"] or "").strip().lower()
            if theme not in {"classic", "paper", "vintage"}:
                return jsonify({"error": "不支持的主题名称"}), 400
            staged["theme"] = theme

        if "density" in data:
            density = str(data["density"] or "").strip().lower()
            if density not in {"comfortable", "compact", "wall"}:
                return jsonify({"error": "不支持的视图密度"}), 400
            staged["density"] = density

        if not staged:
            return jsonify({"error": "没有可保存的字段"}), 400
        database.save_ui_state(staged)
        return jsonify({"state": database.get_ui_state()})

    @app.post("/api/config")
    def api_update_config():
        """Apply settings from the UI.

        Every value is validated into a staging dict first; nothing touches
        ``cfg`` until the whole payload is known to be valid, so a rejected
        field can no longer leave the running process half-updated while the
        database keeps the old value.
        """
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "请求体必须是 JSON 对象"}), 400
        ignored = sorted(set(data) - EDITABLE_FIELDS)
        if ignored:
            logger.info("ignoring non-editable settings keys: %s", ", ".join(ignored))

        staged: dict[str, Any] = {}

        def as_int(key: str, minimum: int, maximum: int) -> str | None:
            if key not in data:
                return None
            try:
                staged[key] = max(minimum, min(maximum, int(data[key])))
            except (TypeError, ValueError):
                return f"{key} 参数不合法"
            return None

        def as_float(key: str, minimum: float, maximum: float) -> str | None:
            if key not in data:
                return None
            try:
                staged[key] = max(minimum, min(maximum, float(data[key])))
            except (TypeError, ValueError):
                return f"{key} 参数不合法"
            return None

        def as_bool(key: str) -> str | None:
            if key not in data:
                return None
            staged[key] = bool(data[key])
            return None

        checks = (
            as_int("proxy_max_edge", 64, 8192),
            as_int("gallery_thumb_size", 96, 4096),
            as_int("proxy_quality", 1, 100),
            as_int("thumb_quality", 1, 100),
            as_int("request_timeout", 5, 3600),
            as_int("model_retries", 0, 10),
            as_int("scan_concurrency", 1, 16),
            as_int("index_concurrency", 1, 32),
            as_int("geocoding_retries", 0, 10),
            as_float("geocoding_interval", 0.1, 10.0),
            # The video block. These keys were listed as editable but never
            # staged, so the dialog accepted a change and silently dropped it -
            # which is why the preview length kept snapping back to its old
            # value. Every numeric setting is clamped here, not in the browser.
            as_float("video_clip_seconds", 2.0, 20.0),
            as_int("video_clip_max_edge", 320, 1920),
            as_int("video_clip_crf", 18, 40),
            as_float("video_poster_ratio", 0.0, 0.5),
            as_int("video_preview_volume", 0, 100),
            as_int("live_volume", 0, 100),
            as_bool("video_clip_audio"),
            as_bool("video_pregenerate"),
            as_bool("merge_live_photos"),
            as_bool("geocoding_fallback"),
            as_int("memory_window_days", 0, 30),
            as_bool("face_recognition_enabled"),
            as_bool("face_auto_scan"),
            as_bool("face_detect_pets"),
            as_float("face_match_threshold", 0.25, 0.75),
            as_int("face_min_cluster_size", 1, 10),
        )
        for error in checks:
            if error:
                return jsonify({"error": error}), 400

        if "video_preview_mode" in data:
            mode = str(data["video_preview_mode"]).strip().lower()
            if mode not in PREVIEW_MODES:
                return jsonify({"error": "video_preview_mode 参数不合法"}), 400
            staged["video_preview_mode"] = mode

        if "video_processing" in data:
            processing = str(data["video_processing"]).strip().lower()
            if processing not in VIDEO_PROCESSING:
                return jsonify({"error": "video_processing 参数不合法"}), 400
            staged["video_processing"] = processing

        if "face_model" in data:
            fm = str(data["face_model"]).strip().lower()
            if fm not in ("buffalo_l", "buffalo_s"):
                return jsonify({"error": "face_model 仅支持 buffalo_l 或 buffalo_s"}), 400
            staged["face_model"] = fm

        if "face_models_dir" in data:
            staged["face_models_dir"] = str(data["face_models_dir"] or "").strip()[:512]
        if "ffmpeg_path" in data:
            staged["ffmpeg_path"] = str(data["ffmpeg_path"] or "").strip()[:512]

        if "api_base_url" in data:
            staged["api_base_url"] = (
                str(data["api_base_url"]).strip() or cfg.api_base_url
            )
        if "api_key" in data:
            staged["api_key"] = str(data["api_key"])
        if "model" in data:
            staged["model"] = str(data["model"]).strip() or cfg.model
        if "system_prompt" in data:
            staged["system_prompt"] = strip_fixed_sections(
                str(data["system_prompt"])
            )
        if "geocoding_provider" in data:
            raw_provider = str(data["geocoding_provider"]).strip().lower()
            if raw_provider not in GEOCODING_PROVIDERS:
                # Anything unrecognised falls back to the free provider, which
                # is what ``reverse_geocode`` itself does - an empty value used
                # to mean "nominatim" and must keep meaning that.
                logger.debug("unknown geocoding provider %r, using nominatim", raw_provider)
                raw_provider = "nominatim"
            staged["geocoding_provider"] = raw_provider
        if "geocoding_api_key" in data:
            staged["geocoding_api_key"] = str(data["geocoding_api_key"])
        if "footprint_default_view" in data:
            fv = str(data["footprint_default_view"]).strip().lower()
            if fv not in FOOTPRINT_VIEW_MODES:
                return jsonify({"error": "footprint_default_view 仅支持 grid 或 map"}), 400
            staged["footprint_default_view"] = fv
        if "map_tile_provider" in data:
            mp = str(data["map_tile_provider"]).strip().lower()
            if mp not in MAP_TILE_PROVIDERS:
                return jsonify({"error": "不支持的地图底图源"}), 400
            staged["map_tile_provider"] = mp
        if "map_tile_url" in data:
            staged["map_tile_url"] = str(data["map_tile_url"] or "").strip()[:512]
        if "map_api_key" in data:
            staged["map_api_key"] = str(data["map_api_key"] or "").strip()[:256]

        for key, value in staged.items():
            setattr(cfg, key, value)
        _save_settings_to_db(database, cfg)
        return jsonify(cfg.to_dict())

    @app.get("/api/prompt-presets")
    def api_prompt_presets():
        """Built-in voices, saved prompts, and the fixed rules.

        The editable half is the style and scoring criteria. The response also
        carries the fixed anti-AI and output-format blocks so the settings
        dialog can show them verbatim without a second request.
        """
        return jsonify(
            {
                "presets": PROMPT_PRESETS,
                "default": "playful",
                "custom": custom_prompt_presets(database),
                "markers": {
                    "anti_ai": ANTI_AI_MARKER,
                    "format": JSON_FORMAT_MARKER,
                    "legacy_format": LEGACY_JSON_FORMAT_MARKER,
                    "preamble": PROMPT_PREAMBLE,
                },
            }
        )

    @app.post("/api/prompt-presets")
    def api_save_prompt_preset():
        """Save the editable part of a prompt under a name.

        Fixed blocks are stripped first, so a pasted full prompt is reduced to
        the part the reader is allowed to own. Saving with an existing name
        updates it; ``id`` targets one entry when a rename is involved.
        """
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "请求内容不合法"}), 400
        name = str(data.get("name") or "").strip()[:30]
        if not name:
            return jsonify({"error": "请给这套提示词起个名字"}), 400
        text = strip_fixed_sections(str(data.get("text") or "")).strip()
        if not text:
            return jsonify({"error": "提示词内容不能为空"}), 400
        if len(text) > MAX_PROMPT_CHARS:
            return (
                jsonify({"error": f"提示词太长（最多 {MAX_PROMPT_CHARS} 字）"}),
                400,
            )

        presets = custom_prompt_presets(database)
        preset_id = str(data.get("id") or "").strip()[:16]
        existing = next(
            (preset for preset in presets if preset_id and preset["id"] == preset_id),
            None,
        )
        if existing is None:
            existing = next(
                (preset for preset in presets if preset["name"] == name), None
            )
        if existing is None:
            if len(presets) >= MAX_CUSTOM_PRESETS:
                return (
                    jsonify(
                        {"error": f"最多保存 {MAX_CUSTOM_PRESETS} 套自定义提示词"}
                    ),
                    400,
                )
            preset = {"id": uuid.uuid4().hex[:8], "name": name, "text": text}
            presets.append(preset)
        else:
            existing["name"] = name
            existing["text"] = text
            preset = existing
        save_custom_prompt_presets(database, presets)
        return jsonify({"preset": preset, "custom": presets})

    @app.delete("/api/prompt-presets/<preset_id>")
    def api_delete_prompt_preset(preset_id: str):
        presets = [
            preset
            for preset in custom_prompt_presets(database)
            if preset["id"] != preset_id
        ]
        save_custom_prompt_presets(database, presets)
        return jsonify({"custom": presets})
