"""
Pillow-based weinke (schedule grid) PNG generation, S3 upload, and DB URL update.

Triggered on Refresh Screen (see app.py lazy listener). Matches color rules from
weinkes/create_weinkes.py highlight_cells().
"""
from __future__ import annotations

import io
import logging
import os
import re
import time
from datetime import date, timedelta
from typing import List, NamedTuple, Optional, Sequence, Tuple

import boto3
from PIL import Image, ImageDraw, ImageFont

from database import DbManager
from database.orm import AO, Master, Region

_LOG = logging.getLogger(__name__)

# Bundled DejaVu (Lambda has no system fonts; see fonts/LICENSE.DejaVu)
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_FONTS_DIR = os.path.join(_MODULE_DIR, "fonts")

# RGB backgrounds — same hex intent as create_weinkes.py (CSS #rrggbb)
BG_DEFAULT = (0, 0, 0)
BG_FORGE = (196, 59, 1)  # #c43b01
BG_SPECIAL_BLUE = (0, 77, 207)  # #004dcf
BG_OPEN = (25, 77, 51)  # #194D33
TEXT_COLOR = (240, 255, 255)  # #F0FFFF
BORDER_COLOR = (240, 255, 255)

FONT_SIZE = 15
BOLD_FONT_SIZE = 18
CELL_PAD = 8
LINE_GAP = 4
MIN_COL_WIDTH = 80
AO_COL_MIN_WIDTH = 140


class StyledLine(NamedTuple):
    """One drawn text line with the font used for metrics and rendering."""

    text: str
    font: ImageFont.ImageFont


def cell_background_color(label: str) -> Tuple[int, int, int]:
    """Return RGB fill for a cell from its display text (matches highlight_cells)."""
    if label is None:
        return BG_DEFAULT
    s = str(label).strip()
    if not s:
        return BG_DEFAULT
    if "The Forge" in s:
        return BG_FORGE
    if ("VQ" in s) or ("AO Launch" in s) or ("24 Hr Beatdown" in s):
        return BG_SPECIAL_BLUE
    # First four chars OPEN (e.g. OPEN!)
    flat = s.replace("\n", "")
    if len(flat) >= 4 and flat[:4].upper() == "OPEN":
        return BG_OPEN
    return BG_DEFAULT


def _week_bounds(today: date) -> Tuple[Tuple[date, date], Tuple[date, date]]:
    """Same Monday–Sunday windows as create_weinkes.py."""
    tomorrow_dow = (today + timedelta(days=1)).weekday()
    current_start = today + timedelta(days=-tomorrow_dow + 1)
    current_end = today + timedelta(days=7 - tomorrow_dow)
    next_start = current_start + timedelta(weeks=1)
    next_end = current_end + timedelta(weeks=1)
    return (current_start, current_end), (next_start, next_end)


def _load_font() -> ImageFont.ImageFont:
    paths = [
        os.path.join(_FONTS_DIR, "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, FONT_SIZE)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_bold_font() -> ImageFont.ImageFont:
    paths = [
        os.path.join(_FONTS_DIR, "DejaVuSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, BOLD_FONT_SIZE)
        except OSError:
            continue
    # Fallback: larger regular face if no bold file found
    for p in [
        os.path.join(_FONTS_DIR, "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]:
        try:
            return ImageFont.truetype(p, BOLD_FONT_SIZE)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> Tuple[int, int]:
    if not text:
        return 0, 0
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_paragraph(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int
) -> List[str]:
    if not text:
        return []
    lines: List[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for w in words[1:]:
            trial = f"{current} {w}"
            if _text_size(draw, trial, font)[0] <= max_width:
                current = trial
            else:
                lines.append(current)
                current = w
        lines.append(current)
    return lines


def _cell_lines(
    draw: ImageDraw.ImageDraw, cell_text: str, font: ImageFont.ImageFont, max_text_width: int
) -> List[str]:
    if not cell_text.strip():
        return [""]
    out: List[str] = []
    for part in cell_text.split("\n"):
        if not part:
            out.append("")
            continue
        out.extend(_wrap_paragraph(draw, part, font, max_text_width))
    return out if out else [""]


def _line_height(font: ImageFont.ImageFont) -> int:
    """Vertical advance for one line using this font."""
    img = Image.new("RGB", (10, 10), BG_DEFAULT)
    draw = ImageDraw.Draw(img)
    _, h = _text_size(draw, "Ay", font)
    size_hint = getattr(font, "size", None)
    if size_hint is None:
        size_hint = FONT_SIZE
    return max(h + LINE_GAP, int(size_hint) + LINE_GAP)


def _wrap_paragraph_styled(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> List[StyledLine]:
    if not text:
        return [StyledLine("", font)]
    wrapped = _wrap_paragraph(draw, text, font, max_width)
    return [StyledLine(line, font) for line in wrapped]


def _styled_cell_lines(
    draw: ImageDraw.ImageDraw,
    cell_text: str,
    font_reg: ImageFont.ImageFont,
    font_bold: ImageFont.ImageFont,
    max_text_width: int,
    row: int,
    col: int,
) -> List[StyledLine]:
    """
    Wrapped lines with per-line font: bold for AO names, day/date headers, Q names;
    regular for location subtitles and event times.
    """
    if not cell_text.strip():
        return [StyledLine("", font_reg)]

    out: List[StyledLine] = []

    if row == 0 and col == 0:
        # "AO" bold, "Location" regular
        parts = cell_text.split("\n")
        for pi, part in enumerate(parts):
            if not part:
                out.append(StyledLine("", font_reg))
                continue
            f = font_bold if pi == 0 else font_reg
            out.extend(_wrap_paragraph_styled(draw, part, f, max_text_width))
        return out if out else [StyledLine("", font_reg)]

    if row == 0 and col > 0:
        # Day and date: all bold
        for part in cell_text.split("\n"):
            if not part:
                out.append(StyledLine("", font_bold))
                continue
            out.extend(_wrap_paragraph_styled(draw, part, font_bold, max_text_width))
        return out if out else [StyledLine("", font_bold)]

    if row > 0 and col == 0:
        # AO name bold; location subtitle regular (first newline separates)
        nl = cell_text.find("\n")
        if nl == -1:
            head, tail = cell_text, ""
        else:
            head, tail = cell_text[:nl], cell_text[nl + 1 :]
        if head.strip():
            out.extend(_wrap_paragraph_styled(draw, head.strip(), font_bold, max_text_width))
        if tail.strip():
            out.extend(_wrap_paragraph_styled(draw, tail.strip(), font_reg, max_text_width))
        return out if out else [StyledLine("", font_reg)]

    # Data cells: Q / special bold; last line if 4-digit time is regular. Supports "\n\n" merged blocks.
    blocks = cell_text.split("\n\n")
    for bi, block in enumerate(blocks):
        if bi > 0:
            out.append(StyledLine("", font_reg))
        block = block.strip()
        if not block:
            continue
        raw_lines = block.split("\n")
        time_idx: Optional[int] = None
        if raw_lines and re.match(r"^\d{4}$", raw_lines[-1].strip()):
            time_idx = len(raw_lines) - 1
        for i, line in enumerate(raw_lines):
            if not line.strip() and i < len(raw_lines) - 1:
                out.append(StyledLine("", font_reg))
                continue
            f = font_reg if time_idx is not None and i == time_idx else font_bold
            out.extend(_wrap_paragraph_styled(draw, line, f, max_text_width))
    return out if out else [StyledLine("", font_reg)]


def _styled_lines_total_height(styled: List[StyledLine]) -> int:
    return sum(_line_height(sl.font) for sl in styled)


def build_week_grid(
    team_id: str, start: date, end: date, aos_by_channel: dict[str, AO]
) -> Tuple[List[str], List[List[str]]]:
    """
    Build header row and body rows for one week. Columns are every calendar day in [start, end].
    """
    events = DbManager.find_records(
        Master,
        [
            Master.team_id == team_id,
            Master.event_date >= start,
            Master.event_date <= end,
        ],
    )
    events.sort(key=lambda e: (e.ao_channel_id or "", e.event_date, e.event_time or ""))

    dates: List[date] = []
    d = start
    while d <= end:
        dates.append(d)
        d += timedelta(days=1)

    if not events:
        return ["Schedule"], [["No events this week"]]

    def ao_sort_key(ch: str) -> str:
        ao = aos_by_channel.get(ch)
        name = (ao.ao_display_name if ao else ch) or ch
        return name.replace("The ", "", 1).lower()

    channels = sorted({e.ao_channel_id for e in events if e.ao_channel_id}, key=ao_sort_key)

    def ao_label(ch: str) -> str:
        ao = aos_by_channel.get(ch)
        if not ao:
            return ch
        sub = (ao.ao_location_subtitle or "").strip()
        base = (ao.ao_display_name or ch).strip()
        return f"{base}\n{sub}".strip() if sub else base

    cells: dict[Tuple[str, date], str] = {}
    for e in events:
        ch = e.ao_channel_id
        if not ch:
            continue
        lbl = _master_row_label(e)
        key = (ch, e.event_date)
        if key in cells:
            cells[key] = cells[key] + "\n\n" + lbl
        else:
            cells[key] = lbl

    headers = ["AO\nLocation"] + [f"{dt.strftime('%a')}\n{dt.strftime('%m/%d')}" for dt in dates]
    rows: List[List[str]] = []
    for ch in channels:
        row = [ao_label(ch)]
        for dt in dates:
            row.append(cells.get((ch, dt), ""))
        rows.append(row)
    return headers, rows


def _master_row_label(m: Master) -> str:
    q = m.q_pax_name
    if q is None or (isinstance(q, str) and not str(q).strip()):
        q = "OPEN!"
    else:
        q = re.sub(r"\s\(([\s\S]*?)\)", "", str(q))
    t = str(m.event_time) if m.event_time is not None else ""
    if m.event_special:
        return f"{q}\n{m.event_special}\n{t}"
    return f"{q}\n{t}"


def render_table_png(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    """
    Draw the schedule grid; image height/width grow with row/column count and text wrapping.
    """
    font_reg = _load_font()
    font_bold = _load_bold_font()
    img = Image.new("RGB", (10, 10), BG_DEFAULT)
    draw = ImageDraw.Draw(img)
    min_line_h = max(_line_height(font_reg), _line_height(font_bold))

    ncols = len(headers)
    nrows = len(rows) + 1  # + header

    # Estimated max text width per column for wrapping (refined after col widths known)
    col_widths = [MIN_COL_WIDTH] * ncols
    col_widths[0] = AO_COL_MIN_WIDTH

    # Iteratively widen columns based on wrapped content
    grid: List[List[str]] = [[h for h in headers]] + [list(r) for r in rows]
    for _ in range(3):
        for c in range(ncols):
            max_w = 0
            for r in range(nrows):
                cell = grid[r][c] if c < len(grid[r]) else ""
                inner_w = max(col_widths[c] - 2 * CELL_PAD, 40)
                styled = _styled_cell_lines(
                    draw, cell, font_reg, font_bold, inner_w, r, c
                )
                for sl in styled:
                    max_w = max(max_w, _text_size(draw, sl.text, sl.font)[0])
            need = max_w + 2 * CELL_PAD
            if c == 0:
                col_widths[c] = max(AO_COL_MIN_WIDTH, need)
            else:
                col_widths[c] = max(MIN_COL_WIDTH, need)

    row_heights: List[int] = []
    for r in range(nrows):
        max_content_h = min_line_h
        for c in range(ncols):
            cell = grid[r][c] if c < len(grid[r]) else ""
            inner_w = max(col_widths[c] - 2 * CELL_PAD, 40)
            styled = _styled_cell_lines(
                draw, cell, font_reg, font_bold, inner_w, r, c
            )
            max_content_h = max(max_content_h, _styled_lines_total_height(styled))
        row_heights.append(max(max_content_h + 2 * CELL_PAD, min_line_h + 2 * CELL_PAD))

    total_w = sum(col_widths) + 1
    total_h = sum(row_heights) + 1
    img = Image.new("RGB", (total_w, total_h), BG_DEFAULT)
    draw = ImageDraw.Draw(img)

    y = 0
    for r in range(nrows):
        x = 0
        h = row_heights[r]
        for c in range(ncols):
            w = col_widths[c]
            cell = grid[r][c] if c < len(grid[r]) else ""
            bg = cell_background_color(cell) if r > 0 and c > 0 else BG_DEFAULT
            if r == 0 or c == 0:
                bg = BG_DEFAULT
            draw.rectangle([x, y, x + w, y + h], fill=bg, outline=BORDER_COLOR, width=1)
            inner_w = max(w - 2 * CELL_PAD, 40)
            styled = _styled_cell_lines(
                draw, cell, font_reg, font_bold, inner_w, r, c
            )
            text_h = _styled_lines_total_height(styled)
            ty = y + (h - text_h) // 2
            for sl in styled:
                tw, _th = _text_size(draw, sl.text, sl.font)
                tx = x + (w - tw) // 2
                draw.text((tx, ty), sl.text, fill=TEXT_COLOR, font=sl.font)
                ty += _line_height(sl.font)
            x += w
        y += h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def upload_to_s3(png_bytes: bytes, bucket: str, key: str) -> str:
    client = boto3.client("s3")
    client.put_object(Bucket=bucket, Key=key, Body=png_bytes, ContentType="image/png")
    return f"https://{bucket}.s3.amazonaws.com/{key}?v={int(time.time())}"


def generate_and_store_weinke(team_id: str, logger: Optional[logging.Logger] = None) -> None:
    """
    Build current + next week PNGs, upload to s3://bucket/weinkes/{team_id}_*.png, update Region URLs.
    No-op if IMAGE_S3_BUCKET is unset (logs warning).
    """
    log = logger or _LOG
    bucket = (os.environ.get("IMAGE_S3_BUCKET") or "").strip()
    if not bucket:
        log.warning("IMAGE_S3_BUCKET not set; skipping weinke generation")
        return

    today = date.today()
    (cur_s, cur_e), (nx_s, nx_e) = _week_bounds(today)

    aos_list = DbManager.find_records(AO, [AO.team_id == team_id])
    aos_map = {a.ao_channel_id: a for a in aos_list if a.ao_channel_id}

    updates: dict = {}
    for start, end, suffix in (
        (cur_s, cur_e, "current_week_weinke"),
        (nx_s, nx_e, "next_week_weinke"),
    ):
        headers, body = build_week_grid(team_id, start, end, aos_map)
        png = render_table_png(headers, body)
        name = f"{team_id}_{suffix}"
        key = f"weinkes/{name}.png"
        url = upload_to_s3(png, bucket, key)
        log.info("Uploaded weinke %s -> %s", key, url)
        if suffix == "current_week_weinke":
            updates[Region.current_week_weinke] = url
        else:
            updates[Region.next_week_weinke] = url
    if updates:
        DbManager.update_record(Region, team_id, updates)
