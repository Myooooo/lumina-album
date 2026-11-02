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
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 64px 24px 96px;
  background: #faf6f0;
  color: #2c241d;
  font-family: "Noto Serif SC", "Songti SC", Georgia, serif;
  line-height: 1.75;
  -webkit-font-smoothing: antialiased;
}
.page { max-width: 720px; margin: 0 auto; }
header.masthead { text-align: center; margin-bottom: 64px; }
header.masthead h1 {
  margin: 0 0 10px;
  font-size: 30px;
  font-weight: 600;
  letter-spacing: 0.06em;
}
header.masthead .sub {
  margin: 0;
  color: #8e8072;
  font-size: 13px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
header.masthead .rule {
  width: 48px;
  height: 1px;
  margin: 26px auto 0;
  background: rgba(130, 100, 70, 0.32);
}
figure.memory {
  margin: 0 0 60px;
  break-inside: avoid;
}
figure.memory img {
  width: 100%;
  height: auto;
  display: block;
  border-radius: 3px;
  box-shadow: 0 12px 34px rgba(60, 40, 20, 0.13), 0 2px 8px rgba(60, 40, 20, 0.06);
}
figure.memory figcaption { margin-top: 20px; }
figure.memory .title {
  margin: 0 0 8px;
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0.01em;
}
figure.memory .caption {
  margin: 0;
  color: #4a3f35;
  font-size: 14.5px;
}
figure.memory .meta {
  margin-top: 12px;
  color: #8e8072;
  font-size: 12px;
  letter-spacing: 0.04em;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
figure.memory .meta .tag {
  display: inline-block;
  margin-right: 8px;
  padding: 1px 9px;
  border: 1px solid rgba(130, 100, 70, 0.2);
  border-radius: 99px;
}
footer.colophon {
  margin-top: 72px;
  padding-top: 22px;
  border-top: 1px solid rgba(130, 100, 70, 0.18);
  color: #8e8072;
  font-size: 12px;
  text-align: center;
  letter-spacing: 0.06em;
}
@media print {
  body { background: #ffffff; padding: 0; }
  figure.memory { page-break-inside: avoid; }
  figure.memory img { box-shadow: none; border: 1px solid rgba(130, 100, 70, 0.16); }
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
                '<p class="caption" style="color:#8e8072">'
                f"（这张照片已经不在原处：{html.escape(photo.filename)}）</p>"
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


def _cjk_font(size: int):
    """A font that can draw Chinese, at ``size``.

    Falls back to Pillow's bitmap default when no CJK face is installed: the export
    then renders Latin text and boxes for the captions, which is worse than right
    but still better than refusing to export at all.
    """
    from PIL import ImageFont

    for path in _CJK_FONT_CANDIDATES:
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


def build_tall_image(
    photos: list[ExportPhoto],
    title: str = "拾光回忆",
    subtitle: str = "",
    width: int = TALL_IMAGE_WIDTH,
) -> Image.Image | None:
    """Stack ``photos`` into one long PNG-ready image, or ``None`` if empty.

    The layout is deliberately vertical and caption-led: a photo, its title and
    its sentence underneath, repeating. That is what reads well when the result is
    posted and viewed at phone width.
    """
    if not photos:
        return None

    from PIL import ImageDraw

    title_font = _cjk_font(38)
    sub_font = _cjk_font(20)
    caption_font = _cjk_font(20)
    meta_font = _cjk_font(17)

    inner = width - _TALL_MARGIN * 2

    # Two passes: measure first, then draw. Building the canvas once avoids the
    # repeated full-image copy that pasting into a growing image costs.
    blocks: list[tuple[Image.Image | None, list[str], list[str]]] = []
    total_height = _TALL_MARGIN

    header_height = 150 if subtitle else 110
    total_height += header_height

    for photo in photos:
        source = _thumbnail_source(photo)
        image = _load_rgb(source, _TALL_PHOTO_MAX) if source else None
        if image is not None:
            ratio = inner / image.width
            image = image.resize(
                (inner, max(1, int(image.height * ratio))), Image.LANCZOS
            )
        caption_lines = _wrap(photo.caption, 24) if photo.caption else []
        meta_parts = [photo.captured] if photo.captured else []
        if photo.score is not None:
            meta_parts.append(f"{float(photo.score):.1f} 分")
        meta_lines = [" · ".join(meta_parts)] if meta_parts else []
        blocks.append((image, caption_lines, meta_lines))

        total_height += _TALL_GAP
        if photo.title:
            total_height += 32
        if image is not None:
            total_height += image.height
        total_height += len(caption_lines) * 30
        total_height += len(meta_lines) * 24

    total_height += _TALL_MARGIN
    total_height = min(total_height, 30000)  # a hard ceiling Pillow can allocate

    canvas = Image.new("RGB", (width, total_height), "#faf6f0")
    draw = ImageDraw.Draw(canvas)

    y = _TALL_MARGIN
    draw.text((width // 2, y), title, font=title_font, fill="#2c241d", anchor="ma")
    y += 52
    if subtitle:
        draw.text((width // 2, y), subtitle, font=sub_font, fill="#8e8072", anchor="ma")
        y += 38
    draw.line((width // 2 - 24, y + 16, width // 2 + 24, y + 16), fill="#c8b4a0", width=1)
    y += header_height - 52

    for photo, (image, caption_lines, meta_lines) in zip(photos, blocks):
        y += _TALL_GAP
        if photo.title:
            draw.text(
                (_TALL_MARGIN, y), photo.title, font=sub_font, fill="#2c241d"
            )
            y += 32
        if image is not None:
            canvas.paste(image, (_TALL_MARGIN, y))
            y += image.height
            image.close()
        for line in caption_lines:
            draw.text((_TALL_MARGIN, y), line, font=caption_font, fill="#4a3f35")
            y += 30
        for line in meta_lines:
            draw.text((_TALL_MARGIN, y), line, font=meta_font, fill="#8e8072")
            y += 24

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
