"""Turn a set of photos into something that can leave the app.

Two formats, and they answer different questions:

*HTML* is the archival one. A single self-contained file - the images are
embedded as data URIs - so it opens from a thumb drive in ten years with no server
and no missing-image placeholders. It is also the one worth printing: the layout
is a vertical timeline that paginates sensibly.

*A tall image* is the sharing one. A phone screenshot of a photo library is what
people actually post, and it needs a single PNG with everything already stacked -
no browser, no scrolling, no "swipe for more".

Both are built from the indexed thumbnails and the model's captions. Nothing here
calls the model; a photo that was never analysed simply has no caption, and the
export still works.
"""

from __future__ import annotations

import base64
import html
import io
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

#: How wide a tall image is, in pixels. Narrow enough to survive being posted to a
#: social app (most cap the long edge but downscale the whole thing equally), wide
#: enough for a caption line to sit next to a thumbnail.
TALL_IMAGE_WIDTH = 1080

#: Gap and padding inside the tall image, in pixels.
_TALL_MARGIN = 48
_TALL_GAP = 28
#: Longest side of the picture inside the tall image.
_TALL_PHOTO_MAX = 620

#: JPEG quality for the embedded copy. 82 keeps a 300px thumbnail around 20 KB,
#: which matters: the HTML carries every image inline, so a 200-photo export is
#: roughly 4 MB rather than 40.
EMBED_QUALITY = 82


@dataclass
class ExportPhoto:
    """One row of the export, already flattened."""

    filename: str
    path: str
    thumbnail_path: str | None = None
    caption: str = ""
    title: str = ""
    tags: list[str] = field(default_factory=list)
    score: float | None = None
    captured: str = ""


def _load_rgb(path: str, max_side: int) -> Image.Image | None:
    """Open ``path`` as RGB, bounded to ``max_side``. ``None`` when unreadable."""
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            width, height = img.size
            longest = max(width, height)
            if longest > max_side:
                scale = max_side / longest
                img = img.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.LANCZOS,
                )
            return img.copy()
    except (OSError, ValueError) as exc:
        logger.debug("cannot read %s for export: %s", path, exc)
        return None


def _thumbnail_source(photo: ExportPhoto) -> str | None:
    """Prefer the small thumbnail, fall back to the original."""
    for candidate in (photo.thumbnail_path, photo.path):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _embedded_data_uri(photo: ExportPhoto, max_side: int = 480) -> str:
    """A base64 JPEG of the photo, or "" when nothing can be read."""
    source = _thumbnail_source(photo)
    if not source:
        return ""
    image = _load_rgb(source, max_side)
    if image is None:
        return ""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=EMBED_QUALITY, optimize=True)
    image.close()
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_HTML_STYLE = """
/* The same tokens the app uses, so a rebuilt memory reads as part of it. */
:root {
  --bg: #f9f6f0;
  --bg-deep: #efe8dc;
  --paper: #fffefb;
  --text: #2c241d;
  --text-secondary: #5a4e44;
  --muted: #8e8072;
  --muted-light: #b5a99c;
  --primary: #b37d56;
  --primary-dark: #8e5d38;
  --primary-light: #f5ebe1;
  --border: rgba(130, 100, 70, 0.13);
  --border-strong: rgba(130, 100, 70, 0.22);
  --shadow-card: 0 10px 30px rgba(60, 40, 20, 0.08), 0 2px 8px rgba(60, 40, 20, 0.04);
  --shadow-lift: 0 20px 45px rgba(60, 40, 20, 0.15), 0 6px 16px rgba(60, 40, 20, 0.06);
  --font-serif: "Songti SC", "Noto Serif SC", "Source Han Serif SC", "STSong", "SimSun", "Baskerville", "Georgia", serif;
  --font-sans: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-hand: "KaiTi", "STKaiti", "Songti SC", "Baskerville", "Segoe Script", cursive, serif;
  /* The app's own curves: strong-out, fast off the mark, long tail into place. */
  --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
  --ease-soft: cubic-bezier(0.32, 0.72, 0, 1);
  --dur-panel: 400ms;
  color-scheme: light;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  padding: 72px 24px 110px;
  /* Warm paper with the faintest grain, matching the album's canvas. */
  background:
    radial-gradient(circle at 20% 12%, rgba(255, 252, 245, 0.9), transparent 46%),
    radial-gradient(circle at 84% 78%, rgba(245, 235, 225, 0.75), transparent 52%),
    linear-gradient(178deg, var(--bg), var(--bg-deep));
  background-attachment: fixed;
  color: var(--text);
  font-family: var(--font-serif);
  line-height: 1.78;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

.page { max-width: 760px; margin: 0 auto; }

/* ---------- Masthead ---------- */

header.masthead {
  text-align: center;
  margin-bottom: 76px;
  animation: rise var(--dur-panel) var(--ease-soft) both;
}

header.masthead .eyebrow {
  margin: 0 0 18px;
  color: var(--primary);
  font-family: var(--font-sans);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.34em;
  text-transform: uppercase;
}

header.masthead h1 {
  margin: 0;
  font-size: 34px;
  font-weight: 600;
  letter-spacing: 0.1em;
  line-height: 1.35;
}

header.masthead .sub {
  margin: 16px 0 0;
  color: var(--muted);
  font-size: 13.5px;
  letter-spacing: 0.06em;
}

header.masthead .rule {
  width: 54px;
  height: 1px;
  margin: 30px auto 0;
  background: linear-gradient(90deg, transparent, var(--border-strong), transparent);
}

header.masthead .count {
  margin: 18px 0 0;
  color: var(--muted-light);
  font-family: var(--font-sans);
  font-size: 11.5px;
  letter-spacing: 0.16em;
}

/* ---------- The polaroid frames ---------- */

figure.memory {
  margin: 0 0 68px;
  break-inside: avoid;
  /* A polaroid is a photo in a paper mount, so the frame is the element and the
     picture sits inside it with even margins - the same construction the gallery
     uses, at a size that suits a document. */
  background: var(--paper);
  padding: 16px 16px 0;
  border-radius: 4px;
  box-shadow: var(--shadow-card);
  /* Each print is placed by hand: the tilt cycles so no two neighbours match. */
  transform: rotate(var(--rot, 0deg));
  transition: transform var(--dur-panel) var(--ease-out),
              box-shadow var(--dur-panel) var(--ease-out);
  animation: printDrop var(--dur-panel) var(--ease-out) both;
}

figure.memory:nth-of-type(4n+1) { --rot: -0.7deg; }
figure.memory:nth-of-type(4n+2) { --rot: 0.6deg; }
figure.memory:nth-of-type(4n+3) { --rot: -0.4deg; }
figure.memory:nth-of-type(4n)   { --rot: 0.8deg; }

figure.memory img {
  width: 100%;
  height: auto;
  display: block;
  border-radius: 2px;
  background: #efe8dc;
}

figure.memory figcaption {
  padding: 20px 8px 26px;
}

figure.memory .title {
  margin: 0 0 10px;
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.015em;
  line-height: 1.6;
}

/* The model's sentence is the keepsake's heart, so it gets the hand face that the
   app uses for the same line on a card. */
figure.memory .caption {
  margin: 0;
  color: var(--text-secondary);
  font-family: var(--font-hand);
  font-size: 15.5px;
  line-height: 1.9;
  letter-spacing: 0.012em;
}

figure.memory .caption::before {
  content: "\201C";
  margin-right: 1px;
  color: var(--primary);
  font-size: 20px;
  line-height: 0;
  vertical-align: -3px;
}

figure.memory .caption::after {
  content: "\201D";
  margin-left: 1px;
  color: var(--primary);
  font-size: 20px;
  line-height: 0;
  vertical-align: -3px;
}

figure.memory .meta {
  margin: 16px 0 0;
  padding-top: 13px;
  border-top: 1px dashed var(--border);
  color: var(--muted);
  font-family: var(--font-sans);
  font-size: 11.5px;
  letter-spacing: 0.045em;
}

figure.memory .meta .tag {
  display: inline-block;
  margin-right: 7px;
  padding: 1px 9px;
  border: 1px solid var(--border);
  border-radius: 99px;
  background: var(--primary-light);
  color: var(--primary-dark);
  font-size: 10.5px;
  letter-spacing: 0.06em;
}

/* A photo whose file went missing. Said plainly instead of showing a broken glyph
   in something meant to be kept. */
figure.memory .missing {
  margin: 0;
  padding: 44px 20px;
  border: 1px dashed var(--border-strong);
  border-radius: 3px;
  color: var(--muted);
  font-family: var(--font-sans);
  font-size: 12.5px;
  text-align: center;
}

/* ---------- Colophon ---------- */

footer.colophon {
  margin-top: 84px;
  padding-top: 26px;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-family: var(--font-sans);
  font-size: 11.5px;
  text-align: center;
  letter-spacing: 0.1em;
}

footer.colophon .mark {
  display: block;
  margin-bottom: 10px;
  color: var(--primary);
  font-family: var(--font-serif);
  font-size: 13px;
  letter-spacing: 0.28em;
}

/* ---------- Motion ---------- */

@keyframes rise {
  from { opacity: 0; transform: translateY(-8px); }
  to   { opacity: 1; transform: translateY(0); }
}

/* A print being laid down, not a slide: it settles from slightly above and a
   touch small, which is how a card should arrive. */
@keyframes printDrop {
  from { opacity: 0; transform: rotate(var(--rot, 0deg)) translateY(14px) scale(0.985); }
  to   { opacity: 1; transform: rotate(var(--rot, 0deg)) translateY(0) scale(1); }
}

@media (hover: hover) and (pointer: fine) {
  figure.memory:hover {
    transform: rotate(0deg) translateY(-4px);
    box-shadow: var(--shadow-lift);
  }
}

/* Motion is decoration here, so it is the first thing to go. */
@media (prefers-reduced-motion: reduce) {
  header.masthead,
  figure.memory {
    animation: none !important;
  }
  figure.memory {
    transform: none;
  }
  figure.memory:hover {
    transform: none;
  }
}

/* ---------- Print: paper, no shadows, no tilt ---------- */

@media print {
  body {
    background: #ffffff;
    padding: 0;
    line-height: 1.7;
  }
  header.masthead {
    animation: none;
    margin-bottom: 48px;
  }
  figure.memory {
    animation: none;
    transform: none;
    box-shadow: none;
    border: 1px solid var(--border);
    page-break-inside: avoid;
    margin-bottom: 40px;
  }
  figure.memory:hover {
    transform: none;
    box-shadow: none;
  }
  footer.colophon {
    margin-top: 48px;
  }
}

@media (max-width: 620px) {
  body { padding: 44px 16px 80px; }
  header.masthead h1 { font-size: 26px; }
  figure.memory { padding: 12px 12px 0; margin-bottom: 48px; }
  figure.memory figcaption { padding: 16px 4px 20px; }
}
"""


def build_html(
    photos: list[ExportPhoto],
    title: str = "拾光回忆",
    subtitle: str = "",
    colophon: str = "",
) -> str:
    """One self-contained HTML document for ``photos``."""
    figures = []
    for photo in photos:
        data_uri = _embedded_data_uri(photo)
        if data_uri:
            media = f'<img src="{data_uri}" alt="{html.escape(photo.filename)}" />'
        else:
            # The file was missing at export time. Say so rather than leaving a
            # broken-image glyph in what is supposed to be a keepsake.
            media = (
                '<p class="missing">'
                f"这张照片已经不在原处：{html.escape(photo.filename)}"
                "</p>"
            )
        meta_bits = []
        if photo.captured:
            meta_bits.append(html.escape(photo.captured))
        if photo.score is not None:
            meta_bits.append(f"{float(photo.score):.1f} 分")
        tags = "".join(
            f'<span class="tag">{html.escape(tag)}</span>' for tag in photo.tags[:5]
        )
        caption = (
            f'<p class="caption">{html.escape(photo.caption)}</p>'
            if photo.caption
            else ""
        )
        heading = (
            f'<p class="title">{html.escape(photo.title)}</p>' if photo.title else ""
        )
        figures.append(
            f"""
    <figure class="memory">
      {media}
      <figcaption>
        {heading}
        {caption}
        <p class="meta">{tags}{" · ".join(meta_bits)}</p>
      </figcaption>
    </figure>"""
        )

    sub_line = (
        f'<p class="sub">{html.escape(subtitle)}</p>' if subtitle else ""
    )
    colophon_line = (
        f'<footer class="colophon">{html.escape(colophon)}</footer>' if colophon else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{html.escape(title)}</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
  <div class="page">
    <header class="masthead">
      <h1>{html.escape(title)}</h1>
      {sub_line}
      <div class="rule"></div>
    </header>
    {"".join(figures)}
    {colophon_line}
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Tall image
# ---------------------------------------------------------------------------


#: Font files tried for the tall image, in order. A CJK-capable face is not
#: optional here: Pillow's bundled bitmap font covers ASCII only, and every caption
#: in this app is Chinese, so `load_default` would draw boxes. Every macOS and
#: Windows install ships at least one of these; Linux gets the usual Noto paths.
_CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",  # Microsoft YaHei
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)


#: Serif faces tried first for the prose. The app sets its captions in a serif and
#: its dates in a sans; a long image that swapped that around would not look like the
#: page it came from.
_SERIF_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\simsun.ttc",  # SimSun
    r"C:\Windows\Fonts\simsunb.ttf",
    r"C:\Windows\Fonts\STSONG.TTF",
    "/System/Library/Fonts/Songti.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc",
)


def _cjk_font(size: int, serif: bool = False):
    """A font that can draw Chinese, at ``size``.

    ``serif`` walks the serif candidates first and falls back to the sans list, so a
    machine with only Microsoft YaHei still renders - just in the other face.

    Falls back to Pillow's bitmap default when no CJK face is installed: the export
    then renders Latin text and boxes for the captions, which is worse than right
    but still better than refusing to export at all.
    """
    from PIL import ImageFont

    groups = (_SERIF_FONT_CANDIDATES, _CJK_FONT_CANDIDATES) if serif else (
        _CJK_FONT_CANDIDATES,
    )
    for group in groups:
        for path in group:
            if not os.path.exists(path):
                continue
            try:
                return ImageFont.truetype(path, size)
            except (OSError, ValueError):
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow before 10.1 has no size argument
        return ImageFont.load_default()


#: The polaroid mount, in pixels at the export's scale.
_TALL_MOUNT = 22          # paper border around the picture
_TALL_FOOT = 26           # the white band under the last line of text
_TALL_PRINT_GAP = 54      # air between prints
_TALL_RADIUS = 4          # the print's own corner
_TALL_TEXT_GAP = 22       # between the picture and the first line
_TALL_TITLE_LINE = 34     # a title line
_TALL_CAPTION_LINE = 34   # a caption line
_TALL_META_BLOCK = 34     # the dashed rule and the meta line
_TALL_MISSING = 120       # the placeholder's height when a file has gone


def _paper_gradient(width: int, height: int) -> Image.Image:
    """The app's canvas: warm paper, a little lighter at the top left."""
    from PIL import Image
    from PIL import ImageDraw as _ImageDraw

    canvas = Image.new("RGB", (width, height), "#f9f6f0")
    draw = _ImageDraw.Draw(canvas)
    # A vertical wash from the paper tone into the deeper tone, which is what the
    # page does with its background gradient.
    top = (0xF9, 0xF6, 0xF0)
    bottom = (0xEF, 0xE8, 0xDC)
    for y in range(height):
        ratio = y / max(1, height - 1)
        colour = tuple(
            int(top[i] + (bottom[i] - top[i]) * ratio * 0.85) for i in range(3)
        )
        draw.line((0, y, width, y), fill=colour)
    return canvas


def _rounded(draw, box, radius: int, fill: str) -> None:
    """A filled rounded rectangle, tolerant of the older Pillow signature."""
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill)
    except (AttributeError, TypeError):
        draw.rectangle(box, fill=fill)


def build_tall_image(
    photos: list[ExportPhoto],
    title: str = "拾光回忆",
    subtitle: str = "",
    width: int = TALL_IMAGE_WIDTH,
) -> Image.Image | None:
    """Stack ``photos`` into one long PNG-ready image, or ``None`` if empty.

    Laid out as a column of polaroid prints: the format is read on a phone, so each
    picture gets the full column and its sentence sits directly under it.
    """
    if not photos:
        return None

    from PIL import Image, ImageDraw

    title_font = _cjk_font(40, serif=True)
    sub_font = _cjk_font(20)
    heading_font = _cjk_font(21, serif=True)
    caption_font = _cjk_font(20, serif=True)
    meta_font = _cjk_font(16)

    print_width = width - _TALL_MARGIN * 2
    inner = print_width - _TALL_MOUNT * 2

    blocks: list[tuple] = []
    total_height = _TALL_MARGIN

    header_height = 210 if subtitle else 170
    total_height += header_height

    for photo in photos:
        source_path = _thumbnail_source(photo)
        image = _load_rgb(source_path, _TALL_PHOTO_MAX) if source_path else None
        if image is not None:
            ratio = inner / image.width
            image = image.resize(
                (inner, max(1, int(image.height * ratio))), Image.LANCZOS
            )
        caption_lines = _wrap(photo.caption, 22) if photo.caption else []
        meta_parts = [photo.captured] if photo.captured else []
        if photo.score is not None:
            meta_parts.append(f"{float(photo.score):.1f} 分")
        meta_line = " · ".join(meta_parts)
        blocks.append((photo, image, caption_lines, meta_line))

        print_height = (
            _TALL_MOUNT * 2
            + (image.height if image else _TALL_MISSING)
            + _TALL_TEXT_GAP
            + _TALL_FOOT
        )
        if photo.title:
            print_height += _TALL_TITLE_LINE
        print_height += len(caption_lines) * _TALL_CAPTION_LINE
        if meta_line:
            print_height += _TALL_META_BLOCK
        total_height += print_height + _TALL_PRINT_GAP

    total_height += _TALL_MARGIN
    total_height = min(total_height, 30000)  # a hard ceiling Pillow can allocate

    canvas = _paper_gradient(width, total_height)
    draw = ImageDraw.Draw(canvas)

    # ---- Masthead -------------------------------------------------------
    y = _TALL_MARGIN + 26
    draw.text(
        (width // 2, y), "拾 光 相 册", font=sub_font, fill="#b37d56", anchor="ma"
    )
    y += 40
    draw.text((width // 2, y), title, font=title_font, fill="#2c241d", anchor="ma")
    y += 62
    if subtitle:
        draw.text(
            (width // 2, y), subtitle, font=sub_font, fill="#8e7072".replace("70", "80"),
            anchor="ma",
        )
        y += 36
    draw.line(
        (width // 2 - 27, y + 12, width // 2 + 27, y + 12), fill="#c8b4a0", width=1
    )
    y = _TALL_MARGIN + header_height

    # ---- The prints -----------------------------------------------------
    for photo, image, caption_lines, meta_line in blocks:
        y += _TALL_PRINT_GAP // 2
        print_height = (
            _TALL_MOUNT * 2
            + (image.height if image else _TALL_MISSING)
            + _TALL_TEXT_GAP
            + _TALL_FOOT
        )
        if photo.title:
            print_height += _TALL_TITLE_LINE
        print_height += len(caption_lines) * _TALL_CAPTION_LINE
        if meta_line:
            print_height += _TALL_META_BLOCK

        # The print: paper mount, then the picture inset by `_TALL_MOUNT`.
        _rounded(
            draw,
            (_TALL_MARGIN, y, _TALL_MARGIN + print_width, y + print_height),
            _TALL_RADIUS,
            "#fffefb",
        )
        cursor = y + _TALL_MOUNT
        if image is not None:
            canvas.paste(image, (_TALL_MARGIN + _TALL_MOUNT, cursor))
            cursor += image.height
            image.close()
        else:
            draw.rectangle(
                (
                    _TALL_MARGIN + _TALL_MOUNT,
                    cursor,
                    _TALL_MARGIN + _TALL_MOUNT + inner,
                    cursor + _TALL_MISSING,
                ),
                fill="#efe8dc",
            )
            draw.text(
                (_TALL_MARGIN + _TALL_MOUNT + inner // 2, cursor + _TALL_MISSING // 2 - 8),
                "这张照片已经不在原处",
                font=meta_font,
                fill="#8e8072",
                anchor="ma",
            )
            cursor += _TALL_MISSING

        text_left = _TALL_MARGIN + _TALL_MOUNT
        text_right = _TALL_MARGIN + print_width - _TALL_MOUNT
        cursor += _TALL_TEXT_GAP
        if photo.title:
            for line in _wrap(photo.title, 20)[:1]:
                draw.text(
                    (text_left, cursor), line, font=heading_font, fill="#2c241d"
                )
            cursor += _TALL_TITLE_LINE
        for line in caption_lines:
            draw.text(
                (text_left, cursor), line, font=caption_font, fill="#4a3f35"
            )
            cursor += _TALL_CAPTION_LINE
        if meta_line:
            cursor += 4
            draw.line(
                (text_left, cursor, text_right, cursor), fill="#e8dfd3", width=1
            )
            draw.text(
                (text_left, cursor + 12), meta_line, font=meta_font, fill="#8e8072"
            )

        y += print_height

    return canvas


def _wrap(text: str, per_line: int) -> list[str]:
    """Break a caption into display lines.

    Counted in characters rather than measured, because the bundled bitmap font
    is fixed-width-ish and an exact measure would need a font file this module
    deliberately does not depend on.
    """
    text = (text or "").strip()
    if not text:
        return []
    return [text[i : i + per_line] for i in range(0, len(text), per_line)][:4]


def tall_image_bytes(image: Image.Image) -> bytes:
    """Encode a stacked image as PNG."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _json_field(value: Any, fallback: Any) -> Any:
    """A JSON column, decoded. Rows from ``Database`` arrive decoded; raw rows don't."""
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def rows_to_export(rows: list[dict[str, Any]]) -> list[ExportPhoto]:
    """Flatten database rows into :class:`ExportPhoto` values."""
    photos = []
    for row in rows:
        exif = _json_field(row.get("exif"), {})
        captured = str(exif.get("datetime_original") or "")[:10].replace(":", "-")
        tags = _json_field(row.get("tags"), [])
        if not isinstance(tags, list):
            tags = []
        photos.append(
            ExportPhoto(
                filename=row.get("filename") or "",
                path=row.get("path") or "",
                thumbnail_path=row.get("thumb_path") or row.get("proxy_path"),
                caption=row.get("reason") or "",
                title=row.get("title") or "",
                tags=[str(tag) for tag in tags],
                score=row.get("score"),
                captured=captured,
            )
        )
    return photos
