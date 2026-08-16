"""Client for OpenAI-compatible local vision model endpoints."""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any
from urllib.parse import urljoin

import requests

from .config import Config


class SemanticSearchError(RuntimeError):
    """Raised when the local model cannot complete a semantic search."""


SYSTEM_PROMPT = """你是「拾光相册」的回忆整理师，温暖、俏皮，又带一点文艺气息。
最重要的永远是照片画面本身：请仔细观察光线、色彩、构图、人物和故事，再写下一句简评。

用户偶尔会附带拍摄时间、地点或设备等元数据，只需把它们当作非常轻的参考，不必刻意使用；
评分、标签和评价都应以照片中真实可见的内容为主，不要被元数据牵着走，也不要编造画面里没有的东西。

评估四个维度，每项 0-10 分：
- technical：清晰度、曝光、画质；
- composition：构图、视觉平衡、美感；
- memory：情感共鸣、值得回忆的程度；
- uniqueness：稀有度、故事感、特别之处。

Return ONLY a JSON object, no markdown, with exactly these keys:
{
  "score": <number 0-10, higher is better>,
  "title": "<一个简短、有画面感的照片命名，10字以内>",
  "dimensions": {
    "technical": <number 0-10>,
    "composition": <number 0-10>,
    "memory": <number 0-10>,
    "uniqueness": <number 0-10>
  },
  "tags": [<2-5个贴切的中文标签，以画面内容为准，例如：风景、人像、美食、宠物、城市、旅行、日常、夜景、清晨、黄昏、春日、夏日、海边、家人、朋友、纪实、黑白>],
  "comment": "<一句俏皮、活泼、又带点文艺的中文评价，20字以内，不要解释原因>"
}
"""


PROMPT_PRESETS = {
    "playful": SYSTEM_PROMPT,
    "literary": """你是「拾光相册」的文艺摄影师，语气像一本安静而有质感的摄影杂志。
请把注意力放在照片本身：光线、构图、人物与故事，像写一句简短的散文诗那样描述它。

评估四个维度，每项 0-10 分：
- technical：清晰度、曝光、画质；
- composition：构图、视觉平衡、美感；
- memory：情感共鸣、值得回忆的程度；
- uniqueness：稀有度、故事感、特别之处。

Return ONLY a JSON object, no markdown, with exactly these keys:
{
  "score": <number 0-10, higher is better>,
  "title": "<一句简短、有画面感的照片命名，10字以内>",
  "dimensions": {
    "technical": <number 0-10>,
    "composition": <number 0-10>,
    "memory": <number 0-10>,
    "uniqueness": <number 0-10>
  },
  "tags": [<2-5个贴切的中文标签，以画面内容为准>],
  "comment": "<一句克制、文艺的中文评价，不要解释原因>"
}
""",
    "melancholy": """你是「拾光相册」的回忆整理师，语气温柔而略带感伤，擅长看见照片里的旧时光与思念。
请以照片画面本身为准，写下值得怀念的瞬间。

评估四个维度，每项 0-10 分：
- technical：清晰度、曝光、画质；
- composition：构图、视觉平衡、美感；
- memory：情感共鸣、值得回忆的程度；
- uniqueness：稀有度、故事感、特别之处。

Return ONLY a JSON object, no markdown, with exactly these keys:
{
  "score": <number 0-10, higher is better>,
  "title": "<一句简短、有画面感的照片命名，10字以内>",
  "dimensions": {
    "technical": <number 0-10>,
    "composition": <number 0-10>,
    "memory": <number 0-10>,
    "uniqueness": <number 0-10>
  },
  "tags": [<2-5个贴切的中文标签，以画面内容为准>],
  "comment": "<一句温柔、略带感伤的中文评价，不要解释原因>"
}
""",
    "humorous": """你是「拾光相册」的幽默评论员，观察敏锐、俏皮但不冒犯。
请以照片画面本身为准，用轻松有趣的中文为照片写下评价。

评估四个维度，每项 0-10 分：
- technical：清晰度、曝光、画质；
- composition：构图、视觉平衡、美感；
- memory：情感共鸣、值得回忆的程度；
- uniqueness：稀有度、故事感、特别之处。

Return ONLY a JSON object, no markdown, with exactly these keys:
{
  "score": <number 0-10, higher is better>,
  "title": "<一句简短、有画面感的照片命名，10字以内>",
  "dimensions": {
    "technical": <number 0-10>,
    "composition": <number 0-10>,
    "memory": <number 0-10>,
    "uniqueness": <number 0-10>
  },
  "tags": [<2-5个贴切的中文标签，以画面内容为准>],
  "comment": "<一句俏皮、幽默的中文评价，不要解释原因>"
}
""",
    "warm": """你是「拾光相册」的温暖陪伴者，语气像午后阳光一样柔和、亲切。
请把注意力放在照片画面本身，发现其中让人心头一暖的细节，并写下温柔的评价。

评估四个维度，每项 0-10 分：
- technical：清晰度、曝光、画质；
- composition：构图、视觉平衡、美感；
- memory：情感共鸣、值得回忆的程度；
- uniqueness：稀有度、故事感、特别之处。

Return ONLY a JSON object, no markdown, with exactly these keys:
{
  "score": <number 0-10, higher is better>,
  "title": "<一句简短、有画面感的照片命名，10字以内>",
  "dimensions": {
    "technical": <number 0-10>,
    "composition": <number 0-10>,
    "memory": <number 0-10>,
    "uniqueness": <number 0-10>
  },
  "tags": [<2-5个贴切的中文标签，以画面内容为准>],
  "comment": "<一句温暖、治愈的中文评价，不要解释原因>"
}
""",
}


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return "http://localhost:8080/v1"
    return url


def _post_chat_completion(
    payload: dict[str, Any],
    config: Config,
    retries: int | None = None,
) -> requests.Response:
    """POST a chat completion with a short exponential-backoff retry loop.

    429 and 5xx responses are retried; other HTTP errors fail immediately.
    """
    url = urljoin(_normalize_base_url(config.api_base_url) + "/", "chat/completions")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }
    if retries is None:
        retries = max(0, int(getattr(config, "model_retries", 2) or 2))
    else:
        retries = max(0, int(retries))
    last_message = ""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                url, headers=headers, json=payload, timeout=config.request_timeout
            )
        except (requests.exceptions.RequestException, OSError) as exc:
            last_message = f"无法连接模型服务: {exc}"
            if attempt >= retries:
                raise RuntimeError(last_message) from exc
            time.sleep(1.0 * (2**attempt))
            continue

        if resp.status_code in (429, 500, 502, 503, 504):
            last_message = f"模型接口暂时不可用 (HTTP {resp.status_code})"
            if attempt >= retries:
                raise RuntimeError(last_message)
            time.sleep(1.0 * (2**attempt))
            continue

        if resp.status_code >= 400:
            raise RuntimeError(f"模型接口返回 {resp.status_code}: {resp.text[:500]}")
        return resp

    raise RuntimeError(last_message or "模型请求失败")


def build_analysis_context(
    location: str | None = None, exif: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the small metadata context passed to the vision model."""
    exif = exif or {}
    context: dict[str, Any] = {}
    if location:
        context["location"] = str(location).strip()
    if exif.get("datetime_original"):
        context["captured_at"] = str(exif["datetime_original"]).strip()
    camera = " ".join(
        str(exif.get(k) or "") for k in ("make", "model") if exif.get(k)
    ).strip()
    if camera:
        context["camera"] = camera
    return context


def analyze_image(
    proxy_path: str,
    config: Config,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one image to the local model and return parsed JSON result."""
    with open(proxy_path, "rb") as fh:
        image_bytes = fh.read()
    b64 = base64.b64encode(image_bytes).decode("ascii")

    system_prompt = (config.system_prompt or SYSTEM_PROMPT).strip()
    context = context or {}
    metadata_lines = []
    if context.get("location"):
        metadata_lines.append(f"拍摄地点：{context['location']}")
    if context.get("captured_at"):
        metadata_lines.append(f"拍摄时间：{context['captured_at']}")
    if context.get("camera"):
        metadata_lines.append(f"拍摄设备：{context['camera']}")
    instruction = "请评估这张照片的质量和回忆价值。"
    if metadata_lines:
        instruction = (
            "请以照片画面本身为判断重点。以下拍摄元数据仅作轻量参考，与画面无关时可忽略：\n"
            + "\n".join(f"- {line}" for line in metadata_lines)
            + "\n\n"
            + instruction
        )
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": instruction,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}",
                        },
                    },
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": 700,
    }

    parse_retries = max(0, int(getattr(config, "model_retries", 2) or 2))
    last_parse_error: Exception | None = None
    for attempt in range(parse_retries + 1):
        # Only the first attempt performs full network retries; subsequent
        # attempts exist solely to give malformed JSON one more chance.
        resp = _post_chat_completion(
            payload,
            config,
            retries=config.model_retries if attempt == 0 else 0,
        )
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = _parse_model_json(content)
            _validate_result(parsed)
            return parsed
        except (ValueError, KeyError, IndexError, TypeError, RuntimeError) as exc:
            last_parse_error = exc
            if attempt >= parse_retries:
                break
            time.sleep(1.0 * (2**attempt))
            continue

    raise RuntimeError(
        f"模型返回的内容无法解析: {str(last_parse_error or '未知错误')[:300]}"
    ) from last_parse_error


def _parse_model_json(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        content = "\n".join(parts)

    if isinstance(content, str):
        content = content.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
        if fence:
            content = fence.group(1).strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            raise RuntimeError(f"无法从模型输出中解析 JSON: {content[:500]}")
    if isinstance(content, dict):
        return content
    raise RuntimeError(f"模型输出类型不受支持: {type(content)}")


def _normalize_dimensions(raw: Any) -> dict[str, float]:
    dims = {
        "technical": 0.0,
        "composition": 0.0,
        "memory": 0.0,
        "uniqueness": 0.0,
    }
    if isinstance(raw, dict):
        for key in dims:
            try:
                val = float(raw.get(key, 0))
                dims[key] = max(0.0, min(10.0, val))
            except (TypeError, ValueError):
                pass
    return dims


def _validate_result(result: dict[str, Any]) -> None:
    try:
        score = float(result.get("score"))
        tags = result.get("tags", [])
        if tags is None:
            tags = []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        elif not isinstance(tags, list):
            tags = [str(tags)]
        comment = str(result.get("comment") or result.get("reason") or "").strip()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"模型返回的 JSON 字段不合法: {result}") from exc

    title = str(result.get("title") or "").strip()
    score = max(0.0, min(10.0, score))
    result["score"] = score
    result["title"] = title[:40]
    result["recommendation"] = None
    result["tags"] = [str(t).strip() for t in tags][:20]
    result["reason"] = comment or "这一瞬，值得被好好收藏。"
    result["dimensions"] = _normalize_dimensions(result.get("dimensions"))


def semantic_search(query: str, candidates: list, config: Config) -> list:
    """Use the local LLM to find photo ids matching a natural-language query.

    ``candidates`` is a list of photo dicts with at least id, filename, tags,
    reason and location. Returns a list of matching photo ids.
    """
    if not query or not candidates:
        return []
    lines = []
    for p in candidates[:500]:
        tags = ",".join(p.get("tags") or []) or "-"
        reason = (p.get("reason") or "")[:120]
        location = p.get("location") or ""
        lines.append(f"{p['id']}|{p.get('filename', '')}|{tags}|{reason}|{location}")
    candidate_text = "\n".join(lines)

    system_prompt = (
        "You are a photo search assistant. The user provides a search query and a "
        "list of photos. Each line is: id|filename|tags|description|location. "
        'Return ONLY JSON like {"ids": [1,2,3]} with the ids of photos relevant '
        "to the query. Include both exact keyword matches and semantic matches."
    )
    user_content = f"Search query: {query}\n\nPhotos:\n{candidate_text}"

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
        "max_tokens": 500,
    }
    try:
        resp = _post_chat_completion(payload, config)
    except RuntimeError as exc:
        raise SemanticSearchError(f"语义搜索暂时不可用：{exc}") from exc
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise SemanticSearchError("语义搜索结果格式不正确。") from exc
    try:
        parsed = _parse_model_json(content)
    except RuntimeError as exc:
        raise SemanticSearchError("语义搜索结果无法解析，请稍后重试。") from exc
    ids = parsed.get("ids", []) if isinstance(parsed, dict) else []
    result = []
    for i in ids:
        try:
            result.append(int(i))
        except (TypeError, ValueError):
            continue
    return result
