"""Client for OpenAI-compatible local vision model endpoints."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Config
from .legacy_prompts import LEGACY_STYLE_PROMPTS, PREVIOUS_STYLE_PROMPTS


class SemanticSearchError(RuntimeError):
    """Raised when the local model cannot complete a semantic search."""


#: The scoring dimensions, in the order the UI shows them.
#:
#: "composition" was retired: it measured much the same thing as "technical",
#: and asking the model for both made it split hairs between two nearly
#: identical numbers. The migration in ``db.py`` averages the old value into
#: ``technical``, and ``_normalize_dimensions`` applies the same rule to a model
#: that still answers with four keys.
DIMENSION_KEYS = ("technical", "memory", "uniqueness")

#: A dimension older builds stored and the prompt no longer asks for.
LEGACY_DIMENSION_KEYS = ("composition",)

#: Markers of the fixed blocks older builds appended to a prompt. They are kept
#: so a prompt saved by one of those builds can be reduced to its editable part.
ANTI_AI_MARKER = "【写作要求：说人话】"
JSON_FORMAT_MARKER = "【输出格式：必须严格遵守】"
LEGACY_JSON_FORMAT_MARKER = (
    "Return ONLY a JSON object, no markdown, with exactly these keys:"
)

#: The framing older full prompts started with. Only used to strip it.
PROMPT_PREAMBLE = """你在帮一个人整理他拍过的照片。你看到的是一张照片，可能还附带拍摄时间、地点或相机信息。
按下面的【评价风格】和【评分标准】给这张照片打分，并起一个标题、写一句简评。
标题和简评会印在照片下面，和照片一起被反复翻看。"""

#: Fixed rules for the wording call. Small local models default to describing
#: the picture; a short, concrete instruction works much better than a long
#: list. The style line is first so the voice is not buried, and these rules
#: follow it so a style cannot switch the anti-caption contract off.
WORDING_RULES = """{style}

不管前面怎么说，下面几条必须遵守：
写一句照片背面的小字，不要描述画面里有什么。
只写看到它时心里冒出来的一句话，20 个字以内。
不要用“这一刻、仿佛、时间、温柔、治愈、岁月、人间烟火、美好、时光”这些词。
不要用照片里的人、动物、东西、光线、影子当开头；先写你的感受。
不编照片里没有的人名、地点、日期。

只输出这句话，不要标题，不要解释。"""

#: Openers cycled across calls. The model's favourite opening is 「原来」; left to
#: itself it starts nearly every photo that way. A different opener per photo
#: keeps an album from reading like one template repeated.
WORDING_OPENERS = ("大概", "其实", "有些", "如果", "有时候", "原来")

#: Default scoring criteria. They are part of the editable prompt: a reader can
#: change what a high score means, but not the JSON shape or the wording rules.
DEFAULT_SCORING_CRITERIA = """- technical（技术）：清晰、曝光、构图；糊一点、暗一点不一定是坏事。
- memory（回忆）：值不值得以后再翻到，有没有想留住的人或瞬间。
- uniqueness（独特）：是不是只有这一次、这个角度才有。
- score（总分）：先想“我会不会留下它”，再给分；9-10 一定留，7-8 值得留，5-6 可有可无，0-4 可以删。"""

STYLE_TEMPLATE = (
    "【评价风格】\n{style}\n\n【评分标准】\n" + DEFAULT_SCORING_CRITERIA
)

#: Voice-only descriptions. Kept short on purpose: the model writes the comment
#: in a separate call, and a long persona starts reading like an instruction to
#: describe the picture.
STYLE_DESCRIPTIONS = {
    "playful": "用打趣的口吻写，允许夸张、允许损一句，但不刻薄。",
    "literary": "用安静、克制的口吻写，像写给自己看的。",
    "humorous": "用一本正经的吐槽口吻写，不要解释笑点。",
    "warm": "用关心人的口吻写，像对家里人说话，别腻。",
    "melancholy": "用舍不得的口吻写，收着一点，不煽情。",
}


def build_style_prompt(style: str) -> str:
    """Render the editable prompt: a voice plus the scoring criteria.

    The fixed wording rules and the scoring JSON are added per request by
    :func:`build_wording_prompt` and :func:`build_scoring_prompt`.
    """
    description = STYLE_DESCRIPTIONS.get(style, STYLE_DESCRIPTIONS["playful"])
    return STYLE_TEMPLATE.format(style=description).strip()


DEFAULT_STYLE_PROMPT = build_style_prompt("playful")

PROMPT_PRESETS = {name: build_style_prompt(name) for name in STYLE_DESCRIPTIONS}


def strip_fixed_sections(prompt: str | None) -> str:
    """Reduce a prompt to its editable part.

    Users can paste a whole prompt saved by an earlier version; only the style
    and criteria survive. This is what makes the fixed rules impossible to
    override by pasting.
    """
    text = (prompt or "").strip()
    if text.startswith(PROMPT_PREAMBLE):
        text = text[len(PROMPT_PREAMBLE) :].lstrip("\n").strip()
    for marker in (ANTI_AI_MARKER, JSON_FORMAT_MARKER, LEGACY_JSON_FORMAT_MARKER):
        found = text.find(marker)
        if found != -1:
            text = text[:found].rstrip()
    return text


def strip_json_format(prompt: str | None) -> str:
    """Backward-compatible name for :func:`strip_fixed_sections`."""
    return strip_fixed_sections(prompt)


def split_editable_prompt(prompt: str | None) -> tuple[str, str]:
    """Split the editable prompt into its voice and scoring criteria.

    A prompt without the markers (an older custom one, or a user who deleted
    the heading) is treated as a voice and uses the default criteria.
    """
    text = strip_fixed_sections(prompt).strip() or DEFAULT_STYLE_PROMPT
    if "【评分标准】" in text:
        style_part, criteria = text.split("【评分标准】", 1)
    else:
        style_part, criteria = text, ""
    style_part = style_part.replace("【评价风格】", "", 1).strip()
    criteria = criteria.strip() or DEFAULT_SCORING_CRITERIA
    if not style_part:
        style_part = STYLE_DESCRIPTIONS["playful"]
    return style_part, criteria


def build_wording_prompt(user_prompt: str | None = None) -> str:
    """The fixed task for the comment call, with the reader's voice in front."""
    style, _criteria = split_editable_prompt(user_prompt)
    # ``replace`` rather than ``format``: a custom style may legitimately contain
    # braces (JSON examples are a common thing to paste).
    return WORDING_RULES.replace("{style}", style, 1)


def build_scoring_prompt(user_prompt: str | None = None) -> str:
    """The fixed scoring/JSON task, with the reader's criteria appended."""
    _style, criteria = split_editable_prompt(user_prompt)
    return f"""给这张照片打分、打标签、起标题。不要写简评。
标题 2-6 个字，不要只写物体或地点，也不要用“温馨、安静、美好、时光、岁月、无声、静默”这类词，不要用“……的瞬间”“……的时光”“……的陪伴”这类短语；像一句小字的题目。
tags 用 2-4 个简短中文词，不要长短语。
评分标准：
{criteria}

【输出格式：必须严格遵守】
只输出一个 JSON 对象，不要 Markdown、不要代码块、不要解释：
{{
  "title": "<2-6 个字的标题>",
  "score": <0 到 10 的数字，可带一位小数>,
  "dimensions": {{
    "technical": <0 到 10 的数字>,
    "memory": <0 到 10 的数字>,
    "uniqueness": <0 到 10 的数字>
  }},
  "tags": ["<中文标签>", "<中文标签>"]
}}
要求：
- 只输出这个 JSON 对象本身，第一个字符是 {{，最后一个字符是 }}。
- tags 是 2-4 个简短中文词的数组，不带 #，不是句子，不能为空。
- score 和 dimensions 的三项都是数字，不是字符串，取值 0-10。
- title、tags 用简体中文。"""


def migrate_legacy_style_prompt(prompt: str | None) -> str | None:
    """Return the new preset when ``prompt`` is an unmodified old stock one.

    Two generations are checked: the first long prompts, and the long
    "humanised" prompts of the immediately previous release. Only an exact
    match is upgraded; an edited prompt is the reader's and is left alone.
    """
    text = strip_fixed_sections(prompt)
    for stock in (LEGACY_STYLE_PROMPTS, PREVIOUS_STYLE_PROMPTS):
        for name, legacy in stock.items():
            if text == legacy:
                return PROMPT_PRESETS[name]
    return None


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return "http://localhost:8080/v1"
    return url


#: Names that resolve to both a v4 and a v6 loopback address.
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


def _loopback_literal(url: str) -> str:
    """Rewrite a loopback *name* to the v4 literal, leaving everything else alone.

    A local model server listens on one family. On Windows ``localhost`` resolves to
    `::1` first, so a v4-only listener makes the client spend a whole connect timeout
    on the v6 address before trying the v4 one - measured at exactly one extra second
    on every probe against a stopped server.

    Only the host of a loopback *name* is rewritten. A real hostname or an existing
    literal is left as written, because there the resolution order is the user's
    configuration and guessing would be wrong.
    """
    parts = urlsplit(url)
    if parts.hostname and parts.hostname.lower() in _LOOPBACK_NAMES:
        netloc = parts.netloc.replace(parts.hostname, "127.0.0.1", 1)
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def probe_endpoint(config: Config, timeout: float = 3.0) -> tuple[bool, str]:
    """Check that the model endpoint answers before starting a long run.

    Without this, a run queued against a stopped server spends the full retry
    budget on every photo - measured at ~15 s each - and writes a failure row for
    all of them. One cheap request up front turns that into an immediate, clear
    refusal.

    The probe asks for ``GET <base>/models``, which every OpenAI-compatible
    server (LM Studio, Ollama, llama.cpp, vLLM) implements and which costs no
    tokens. A server that does not expose it is not treated as broken: any HTTP
    answer at all proves something is listening, so only a transport failure
    counts as "down".

    Returns ``(reachable, message)``.
    """
    url = urljoin(_normalize_base_url(config.api_base_url) + "/", "models")
    headers = {}
    api_key = getattr(config, "api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # One attempt, and no address-family fallback cost.
    #
    # Two things made this slower than its timeout, both measured rather than
    # guessed: urllib3 retries a refused connection by default, and on Windows
    # `localhost` resolves to `::1` first, so a v4-only server pays a full
    # connect timeout on the v6 address before the v4 attempt is made. Together
    # they turned a 1 s probe into 2 s.
    #
    # The retry policy is disabled below. For the family penalty the fix is to
    # pick the literal the server actually listens on when the host is a
    # loopback name - the endpoints this app talks to are all local.
    probe_url = _loopback_literal(url)
    retry = Retry(total=0, connect=0, read=0, redirect=0)
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    try:
        resp = session.get(probe_url, headers=headers, timeout=timeout)
    except (requests.exceptions.RequestException, OSError) as exc:
        return False, f"无法连接模型服务 {config.api_base_url}：{exc}"
    finally:
        session.close()
    if resp.status_code >= 500:
        return False, f"模型服务暂时不可用（HTTP {resp.status_code}）"
    return True, ""


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


def _metadata_text(context: dict[str, Any] | None) -> str:
    """Render the optional capture metadata as a short reference block."""
    context = context or {}
    lines = []
    if context.get("location"):
        lines.append(f"拍摄地点：{context['location']}")
    if context.get("captured_at"):
        lines.append(f"拍摄时间：{context['captured_at']}")
    if context.get("camera"):
        lines.append(f"拍摄设备：{context['camera']}")
    if not lines:
        return ""
    return "拍摄信息（只作参考，不要复述）：\n" + "\n".join(f"- {line}" for line in lines)


def _image_messages(system_prompt: str, proxy_path: str, instruction: str) -> list[dict[str, Any]]:
    """One system message and one user message with the image, then the text.

    The image comes first and the instruction after it: a small vision model
    pays much more attention to the text closest to the image, and that is where
    "only write a reaction" has to land.
    """
    with open(proxy_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
                {"type": "text", "text": instruction},
            ],
        },
    ]


def _clean_wording(content: Any) -> str:
    """Turn whatever the wording call returned into one usable sentence.

    The model is asked for the sentence alone, but small models sometimes wrap
    it in a code fence, a label (``简评：``), or a title line. Those are peeled
    off; a JSON object or an empty answer is treated as a failure and retried.
    """
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    picked = ""
    for raw_line in lines:
        line = raw_line.lstrip("*# ").strip()
        if line.startswith(("标题：", "标题:")):
            continue
        if line.startswith(("简评：", "简评:", "评论：", "评论:")):
            picked = re.split(r"[:：]", line, maxsplit=1)[-1].strip()
            break
        if not picked:
            picked = line
    picked = picked.strip().strip("“”\"'")
    picked = re.sub(r"^[*#\s]+", "", picked).strip()
    picked = re.sub(r"\s+", " ", picked)
    if not picked or picked.startswith(("{", "[")):
        return ""
    return picked[:60]


def _wording_opener(proxy_path: str) -> str:
    """Pick the opening word for this photo, stable across re-runs."""
    digest = hashlib.sha1(proxy_path.encode("utf-8", "replace")).digest()
    return WORDING_OPENERS[digest[0] % len(WORDING_OPENERS)]


def _request_wording(
    proxy_path: str, config: Config, metadata: str
) -> str:
    """Ask for the one-line comment; the only call that writes prose."""
    instruction = f"这次请用“{_wording_opener(proxy_path)}”开头，只输出一句话。"
    if metadata:
        instruction = f"{instruction}\n{metadata}"
    payload = {
        "model": config.model,
        "messages": _image_messages(
            build_wording_prompt(config.system_prompt), proxy_path, instruction
        ),
        "temperature": 0.4,
        "max_tokens": 160,
    }
    parse_retries = max(0, int(getattr(config, "model_retries", 2) or 2))
    last_error: Exception | None = None
    for attempt in range(parse_retries + 1):
        resp = _post_chat_completion(
            payload,
            config,
            retries=config.model_retries if attempt == 0 else 0,
        )
        try:
            data = resp.json()
            wording = _clean_wording(data["choices"][0]["message"]["content"])
            if wording:
                return wording
            raise RuntimeError("模型没有写出简评")
        except (ValueError, KeyError, IndexError, TypeError, RuntimeError) as exc:
            last_error = exc
            if attempt >= parse_retries:
                break
            time.sleep(1.0 * (2**attempt))
    raise RuntimeError(
        f"模型没有写出可用的简评: {str(last_error or '未知错误')[:300]}"
    ) from last_error


def _request_scoring(
    proxy_path: str, config: Config, metadata: str
) -> dict[str, Any]:
    """Ask for score, dimensions, tags and title; never for prose."""
    instruction = "按 JSON 输出。"
    if metadata:
        instruction = f"{instruction}\n{metadata}"
    payload = {
        "model": config.model,
        "messages": _image_messages(
            build_scoring_prompt(config.system_prompt), proxy_path, instruction
        ),
        "temperature": 0.1,
        "max_tokens": 700,
    }
    parse_retries = max(0, int(getattr(config, "model_retries", 2) or 2))
    last_error: Exception | None = None
    for attempt in range(parse_retries + 1):
        resp = _post_chat_completion(
            payload,
            config,
            retries=config.model_retries if attempt == 0 else 0,
        )
        try:
            data = resp.json()
            parsed = _parse_model_json(data["choices"][0]["message"]["content"])
            _validate_result(parsed, require_comment=False)
            return parsed
        except (ValueError, KeyError, IndexError, TypeError, RuntimeError) as exc:
            last_error = exc
            if attempt >= parse_retries:
                break
            time.sleep(1.0 * (2**attempt))
    raise RuntimeError(
        f"模型返回的评分不符合格式要求: {str(last_error or '未知错误')[:300]}"
    ) from last_error


def analyze_image(
    proxy_path: str,
    config: Config,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyse one image in two calls: a human comment, then the score.

    One JSON call made small local models write captions: by the time they had
    to emit score, title, dimensions and tags, the comment had turned into a
    plain description of the picture. Asking for the prose alone first, and the
    structured fields in a second request, is what finally produced reactions
    instead of captions with the same 9B model.
    """
    metadata = _metadata_text(context)
    wording = _request_wording(proxy_path, config, metadata)
    scoring = _request_scoring(proxy_path, config, metadata)
    result: dict[str, Any] = dict(scoring)
    result["comment"] = wording
    result["reason"] = wording
    _validate_result(result)
    return result


def visual_search(
    query: str,
    candidates: list[dict[str, Any]],
    config: Config,
    batch_size: int = 4,
    max_candidates: int = 24,
) -> tuple[list[int], int]:
    """Show the pictures to the model and ask which ones match ``query``.

    The text-only search can only reason about captions and tags, so "找到那张
    海边逆光的照片" matches nothing unless a caption happened to use those words. A
    vision model can look.

    The cost is the reason for every limit here: each image is a real request, so
    the candidates are capped, sent a few at a time, and the caller is expected to
    have narrowed the list first (by tag, year, place). Returns ``(ids, checked)``
    so the UI can say how much was actually looked at rather than implying the
    whole library was scanned.
    """
    selected = [p for p in candidates if p.get("thumb_path") or p.get("proxy_path")]
    selected = selected[: max(1, max_candidates)]
    if not query.strip() or not selected:
        return [], 0

    matches: list[int] = []
    checked = 0
    step = max(1, min(batch_size, 8))

    for start in range(0, len(selected), step):
        batch = selected[start : start + step]
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"搜索条件：{query.strip()}\n\n"
                    "下面是几张照片，按顺序编号。请只根据画面本身判断哪些照片"
                    "真正符合这个条件，宁少勿多。"
                ),
            }
        ]
        # Only the photos that could be read go into the batch, and the position
        # maps to *that* list. Keeping unreadable ones in the numbering would let
        # the model's "第 2 张" point at a photo it never saw.
        shown: list[dict[str, Any]] = []
        for photo in batch:
            path = photo.get("thumb_path") or photo.get("proxy_path")
            try:
                with open(path, "rb") as handle:
                    encoded = base64.b64encode(handle.read()).decode("ascii")
            except OSError:
                continue
            shown.append(photo)
            content.append({"type": "text", "text": f"第 {len(shown)} 张："})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                }
            )
        checked += len(batch)
        if not shown:
            continue
        # Answer in *positions*, not database ids. The model never sees an id, and
        # asking it to echo one it cannot read produced matches that included
        # every photo in the batch; a position is something it can actually count.
        content.append(
            {
                "type": "text",
                "text": (
                    '只返回 JSON：{"match": [符合的序号, ...]}。'
                    "序号从 1 开始。没有符合的就返回 {\"match\": []}。不要解释。"
                ),
            }
        )
        payload = {
            "model": config.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0,
            "max_tokens": 300,
        }
        try:
            resp = _post_chat_completion(payload, config)
            text = resp.json()["choices"][0]["message"]["content"]
            parsed = _parse_model_json(text)
        except (RuntimeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise SemanticSearchError(f"视觉搜索暂时不可用：{exc}") from exc
        found = parsed.get("match", []) if isinstance(parsed, dict) else []
        for value in found:
            try:
                position = int(value)
            except (TypeError, ValueError):
                continue
            # Only a position that was actually shown may match: the numbering the
            # model answered against is `shown`, not the raw batch.
            if 1 <= position <= len(shown):
                pid = shown[position - 1]["id"]
                if pid not in matches:
                    matches.append(pid)

    return matches, checked


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
    """Clamp the three scoring dimensions, folding in any legacy "composition".

    A model that still answers with four keys (an old prompt, or a system prompt
    the user wrote before the merge) is not wrong about the photo, it is just
    using the previous shape: its composition score belongs to technical, so it
    is averaged in rather than dropped. The same rule runs over stored rows in
    ``repair.repair_dimension_keys`` and over API payloads in
    ``server._clean_dimensions``, so a photo scores the same whichever path it
    arrives through.
    """
    dims = {key: 0.0 for key in DIMENSION_KEYS}
    if not isinstance(raw, dict):
        return dims
    for key in dims:
        try:
            val = float(raw.get(key, 0))
            dims[key] = max(0.0, min(10.0, val))
        except (TypeError, ValueError):
            pass
    legacy_values = []
    for key in LEGACY_DIMENSION_KEYS:
        if key not in raw:
            continue
        try:
            value = float(raw.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        # Clamped here as well as at the top, so a wild model answer cannot drag
        # a good technical score out of range.
        legacy_values.append(max(0.0, min(10.0, value)))
    if legacy_values:
        dims["technical"] = round(
            (dims["technical"] + sum(legacy_values)) / (len(legacy_values) + 1), 2
        )
    return dims


def _validate_result(
    result: dict[str, Any], require_comment: bool = True
) -> None:
    """Normalise a model result, rejecting output that breaks the contract.

    The scoring call returns four keys; the comment comes from a separate call.
    ``require_comment`` is False for the scoring half, so a missing comment is
    not treated as a format error there.
    """
    if not isinstance(result, dict):
        raise RuntimeError("模型返回的不是 JSON 对象")
    required = ["title", "score", "dimensions", "tags"]
    if require_comment:
        required.append("comment")
    missing = [key for key in required if key not in result]
    if missing:
        raise RuntimeError(f"模型返回缺少字段: {', '.join(missing)}")

    try:
        score = float(result["score"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("score 不是数字") from exc

    dimensions = result.get("dimensions")
    if not isinstance(dimensions, dict):
        raise RuntimeError("dimensions 不是对象")
    for key in DIMENSION_KEYS:
        if key not in dimensions:
            raise RuntimeError(f"dimensions 缺少 {key}")
        try:
            float(dimensions[key])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"dimensions.{key} 不是数字") from exc

    raw_tags = result.get("tags")
    if isinstance(raw_tags, str):
        # A comma-separated string is the one deviation worth forgiving: the
        # meaning is unambiguous and it is common on small local models.
        tags = [item.strip() for item in re.split(r"[,，]", raw_tags) if item.strip()]
    elif isinstance(raw_tags, list):
        tags = [str(item).strip() for item in raw_tags if str(item).strip()]
    else:
        raise RuntimeError("tags 不是数组")
    if not tags:
        raise RuntimeError("tags 为空")

    title = str(result.get("title") or "").strip()
    if not title:
        raise RuntimeError("title 为空")
    comment = ""
    if require_comment:
        comment = str(result.get("comment") or result.get("reason") or "").strip()
        if not comment:
            raise RuntimeError("comment 为空")

    result["score"] = max(0.0, min(10.0, score))
    result["title"] = title[:40]
    result["tags"] = tags[:5]
    if require_comment:
        result["reason"] = comment[:200]
        result["comment"] = comment[:200]
    result["dimensions"] = _normalize_dimensions(dimensions)


#: How many candidate rows ``semantic_search`` puts in front of the model.
#: Longer prompts do not help - the model cannot scan thousands of lines - so a
#: caller should not read more rows from the database than this.
SEMANTIC_CANDIDATE_LIMIT = 500


def semantic_search(query: str, candidates: list, config: Config) -> list:
    """Use the local LLM to find photo ids matching a natural-language query.

    ``candidates`` is a list of photo dicts with at least id, filename, tags,
    reason and location. Returns a list of matching photo ids.
    """
    if not query or not candidates:
        return []
    lines = []
    for p in candidates[:SEMANTIC_CANDIDATE_LIMIT]:
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
