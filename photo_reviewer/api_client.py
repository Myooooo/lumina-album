"""Client for OpenAI-compatible local vision model endpoints."""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Config


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

#: Start of the fixed "write like a person" rules. A user prompt is never
#: trusted to carry these: they are appended to every request, so switching the
#: style can never switch off the anti-AI rules.
ANTI_AI_MARKER = "【写作要求：说人话】"

#: Start of the fixed output-format block. Also the marker used to reduce a
#: pasted full prompt back to its editable part.
JSON_FORMAT_MARKER = "【输出格式：必须严格遵守】"

#: The marker older builds shipped. Still stripped from stored prompts and from
#: a PHOTO_SYSTEM_PROMPT written before this version.
LEGACY_JSON_FORMAT_MARKER = (
    "Return ONLY a JSON object, no markdown, with exactly these keys:"
)

#: Short, fixed framing. The style/criteria below it are the part a user edits.
PROMPT_PREAMBLE = """你在帮一个人整理他拍过的照片。你看到的是一张照片，可能还附带拍摄时间、地点或相机信息。
按下面的【评价风格】和【评分标准】给这张照片打分，并起一个标题、写一句简评。
标题和简评会印在照片下面，和照片一起被反复翻看。"""

#: Fixed anti-AI rules. Kept out of the editable prompt on purpose: "de-AI-ify"
#: is a product rule, not a style the reader can turn off.
ANTI_AI_GUIDE = ANTI_AI_MARKER + """
以下规则优先于上面的风格要求，标题和简评都必须遵守。

1. 写具体的，不写抽象的。
   不写“治愈”“温柔”“美好”“时光”“岁月”“人间烟火”这类空词。写画面里真实存在的一个细节：
   一个动作、一件东西、一束光、一个表情、当时的声音或气味。
2. 说感受，不做说明。
   不要解释照片“展现了什么”“记录了怎样的瞬间”，也不要替人总结意义。
3. 不堆形容词，不叠比喻。
   一句里最多一个比喻；不用“仿佛/宛如/像是/好似”，不用“让人不禁”“令人动容”。
4. 不升华，不抒情收尾。
   不要“这一刻，时间仿佛静止”“愿岁月温柔以待”这类句子；说完就走。
5. 像聊天，允许口语。
   可以用“啊”“呢”“吧”“就”“还挺”，可以短句、半句、不完整；不要排比，不要对仗。
6. 不编事实。
   不写照片里看不见的人名、地点、日期、关系；不确定就不写。
7. 标题和简评不要重复；标题不解释画面，简评不复述标题。
8. 下面这些 AI 套词不要用：仿佛、宛如、好似、这一刻、让人不禁、治愈、温柔了岁月、时光、岁月、
   人间烟火、诗和远方、定格、见证、诉说着、氛围感、松弛感。

反例（不要这样写）：
- “阳光洒在毛茸茸的鼻尖，这一刻的拥抱比春天还暖。”（比喻 + 升华 + 套话）
- “画面中，一位老人静静地坐在长椅上，仿佛在回忆往事。”（说明 + 想象）
- “海边的黄昏，治愈了所有的疲惫。”（空词 + 替人总结）

正例（可以这样写）：
- 标题“等风”，简评“它一直盯着海面，耳朵被风吹得往后翻。”
- 标题“落灰”，简评“鞋边蹭了一道白，没人去擦。”"""

#: Fixed output contract. The parser is strict about the five keys and their
#: types, so the prompt states them once, unambiguously, at the very end.
JSON_FORMAT_SUFFIX = JSON_FORMAT_MARKER + """
只输出一个 JSON 对象。不要 Markdown、不要代码块、不要解释、不要任何前后缀文字。
必须且只能包含以下 5 个键，键名、层级和类型都不能改：
{
  "score": <0 到 10 的数字，可带一位小数，不要加引号>,
  "title": "<标题，不超过 10 个汉字，不要引号、书名号，不要以标点结尾>",
  "dimensions": {
    "technical": <0 到 10 的数字>,
    "memory": <0 到 10 的数字>,
    "uniqueness": <0 到 10 的数字>
  },
  "tags": ["<中文标签>", "<中文标签>"],
  "comment": "<简评，不超过 20 个汉字，不要换行、不要引号>"
}
要求：
- 只输出这个 JSON 对象本身，第一个字符是 {，最后一个字符是 }。
- tags 是 2-5 个简短中文词的数组，不带 #，不是句子，不能为空。
- score 和 dimensions 的三项都是数字，不是字符串，取值 0-10。
- title、tags、comment 用简体中文。"""

#: The editable half of the prompt: who is speaking, and what the scores mean.
STYLE_TEMPLATE = """【评价风格】
{style}

【评分标准】
- technical（技术）：清晰、曝光、色彩、构图。糊一点、暗一点不一定是坏事，要看它有没有毁掉这张照片的味道。
- memory（回忆）：值不值得以后再翻到。有没有一个具体的人、事、物、表情或瞬间，让人愿意把它留下。
- uniqueness（独特）：这个画面是不是只有这一次、这个角度、这个人才会有；少见、意外、有故事感都可以加分。
- score（总分）：先回答“我会不会留下它”，再给分，不要直接拿三项平均。
  9-10：会立刻留下，甚至想洗出来；
  7-8：值得留着，偶尔会翻到；
  5-6：还行，留着不亏，删了也不可惜；
  0-4：可以直接删。"""

#: Voice-only descriptions. The scoring block is shared by every preset, so a
#: new voice does not have to repeat (or accidentally change) the criteria.
STYLE_DESCRIPTIONS = {
    "playful": (
        "说话像朋友群里随手发的语音：反应快，爱打岔，偶尔冒一句欠揍的实话，但不刻薄。"
    ),
    "literary": (
        "话不多，句子短，像在照片背面写一行小字。不抒情，不解释，只留下一个画面或一个动作。"
    ),
    "humorous": (
        "专挑画面里好笑、别扭、意外的地方，一本正经地吐槽，不解释笑点，也不拿人开恶意玩笑。"
    ),
    "warm": (
        "像家里人翻到旧照片时说的那句“你看那时候……”。语气软，但不腻，不替人感动。"
    ),
    "melancholy": (
        "看到旧东西会停一下，话里带一点舍不得和走神，但忍着不煽情，也不把话说满。"
    ),
}


def build_style_prompt(style: str) -> str:
    """Render the editable prompt: a voice plus the scoring criteria.

    Fixed rules (anti-AI and output format) are deliberately absent; they are
    appended by :func:`build_system_prompt` on every request.
    """
    description = STYLE_DESCRIPTIONS.get(style, STYLE_DESCRIPTIONS["playful"])
    return STYLE_TEMPLATE.format(style=description).strip()


DEFAULT_STYLE_PROMPT = build_style_prompt("playful")

PROMPT_PRESETS = {name: build_style_prompt(name) for name in STYLE_DESCRIPTIONS}

#: The stock prompts earlier builds shipped. A stored prompt that still equals
#: one of these is a cached stock choice, not a customisation, so startup can
#: upgrade it to the current text instead of leaving a user on an old voice.
_LEGACY_STYLE_TEMPLATE = """你是「拾光相册」的{style}
你不是在写图片说明，而是在陪朋友一起翻旧相册，看到某张照片时，脱口而出的那句心里话。

先仔细观察照片：光线、色彩、构图、人物、发生的事，然后打分、命名、写评论。
用户偶尔会附带拍摄时间、地点或设备等元数据，只当很轻的参考；一切以照片里真实可见的内容为准，不要被元数据牵着走。

【写评论的铁律】
1. 拒绝"看图说话"：不要罗列画面元素，例如"蓝天、白云、雪山、湖泊"。
2. 拒绝万能开头：不要用"这张照片""画面中""记录了""定格了""展现了""拍摄了"。
3. 写"感受"而不是"说明"：可以写当时的氛围、温度、气味、声音、情绪，
   一个微小的联想、想对画面里的人说的话、想回到那一刻的冲动。
4. 感受和联想可以自由，但不要编造具体事实（人名、日期、地点、事件）。
5. 语气像朋友随口聊天，自然、口语、有情绪，不要客观冷静的旁白。
6. 允许留白和私人感，不必完整、不必解释原因、不必总结升华。
7. 不要堆砌形容词，不要为了文艺而文艺。
8. 必须符合你的人设和风格。

【标题】
像电影截图名或诗里的一行，有画面、有想象空间；
不要写"某人/某物/某地"式的说明，标题和评论也不要重复。

【评分维度】每项 0-10 分：
- technical：清晰度、曝光、画质，以及构图与视觉平衡；
- memory：情感共鸣、让人想保存或想回到那一刻的程度；
- uniqueness：稀有度、故事感、特别之处。
"""

_LEGACY_STYLE_DESCRIPTIONS = {
    "playful": (
        "回忆整理师，活泼又带一点文艺气息。"
        "说话轻快，爱用感叹和设问，会开玩笑但不轻浮。"
    ),
    "literary": (
        "文艺摄影师，语气像一本安静而有质感的摄影杂志。"
        "句子干净、克制、有留白，像给照片配的一行诗。"
    ),
    "melancholy": (
        "回忆整理师，语气温柔而略带感伤，擅长看见旧时光与思念。"
        "话里带着一点舍不得和怀念，但不哀怨、不煽情过头。"
    ),
    "humorous": (
        "幽默评论员，观察敏锐、俏皮但不冒犯。"
        "出其不意、一针见血，让人会心一笑。"
    ),
    "warm": (
        "温暖陪伴者，语气像午后阳光一样柔和、亲切。"
        "像家人随口说的安慰或夸奖，稳稳地接住那一刻。"
    ),
}

LEGACY_STYLE_PROMPTS = {
    name: _LEGACY_STYLE_TEMPLATE.format(style=description).strip()
    for name, description in _LEGACY_STYLE_DESCRIPTIONS.items()
}


def strip_fixed_sections(prompt: str | None) -> str:
    """Reduce a prompt to its editable part.

    Users can paste a whole prompt (including the fixed blocks, or one written
    before this version); only the style and criteria survive. This is what
    makes the fixed rules impossible to override by pasting.
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


def migrate_legacy_style_prompt(prompt: str | None) -> str | None:
    """Return the new preset when ``prompt`` is an unmodified old stock one.

    Only an exact match is upgraded: a prompt the reader edited (even by a
    character) is theirs and is left alone.
    """
    text = strip_fixed_sections(prompt)
    for name, legacy in LEGACY_STYLE_PROMPTS.items():
        if text == legacy:
            return PROMPT_PRESETS[name]
    return None


def build_system_prompt(base_prompt: str | None = None) -> str:
    """Build the complete system prompt.

    Order matters: the editable voice/criteria come first, then the fixed rules
    (which say they win over the voice), then the output contract last. A caller
    cannot opt out of the last two blocks.
    """
    style = strip_fixed_sections(base_prompt).strip() or DEFAULT_STYLE_PROMPT
    return f"{PROMPT_PREAMBLE}\n\n{style}\n\n{ANTI_AI_GUIDE}\n\n{JSON_FORMAT_SUFFIX}"


SYSTEM_PROMPT = build_system_prompt(DEFAULT_STYLE_PROMPT)


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


def analyze_image(
    proxy_path: str,
    config: Config,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one image to the local model and return parsed JSON result."""
    with open(proxy_path, "rb") as fh:
        image_bytes = fh.read()
    b64 = base64.b64encode(image_bytes).decode("ascii")

    system_prompt = build_system_prompt(config.system_prompt)
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
        f"模型返回的内容不符合格式要求: {str(last_parse_error or '未知错误')[:300]}"
    ) from last_parse_error


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


def _validate_result(result: dict[str, Any]) -> None:
    """Normalise a model result, rejecting output that breaks the contract.

    The prompt asks for exactly five keys. A missing key or a wrong container
    type raises, which makes :func:`analyze_image` spend one of its retries
    instead of writing a half-empty row. Numbers are accepted as numeric
    strings (models sometimes quote them) but everything the UI reads is
    normalised into the documented shape.

    Text lengths and ranges are clamped here, not only asked for in the prompt:
    a model that ignores "20 字以内" must not be able to push a paragraph into
    the caption.
    """
    if not isinstance(result, dict):
        raise RuntimeError("模型返回的不是 JSON 对象")
    missing = [
        key
        for key in ("score", "title", "dimensions", "tags", "comment")
        if key not in result
    ]
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
    comment = str(result.get("comment") or result.get("reason") or "").strip()
    if not comment:
        raise RuntimeError("comment 为空")

    result["score"] = max(0.0, min(10.0, score))
    result["title"] = title[:40]
    result["tags"] = tags[:5]
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
