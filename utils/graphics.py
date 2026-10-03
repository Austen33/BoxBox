"""Data-driven reply graphics (drawn with Pillow, never AI-generated).

Every name, position and colour on the image comes from the data passed in:
driver codes/surnames and team colours from F1's timing feed, positions from
the official classification. Nothing is guessed, so nothing can be invented.
"""

import io
import os

import matplotlib
from PIL import Image, ImageDraw, ImageFont

_FONT_DIR = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
_BOLD = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")
_REG = os.path.join(_FONT_DIR, "DejaVuSans.ttf")

BG = "#0f1115"
CARD = "#1b1f27"
CARD_HI = "#24201b"
TEXT = "#f2f2f2"
MUTED = "#9aa0aa"
AMBER = "#ffb020"
GREEN = "#2ecc71"
PAPAYA = "#ff8000"


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _hex(c: str | None, default: str = "#808080") -> str:
    if not c:
        return default
    c = c.strip().lstrip("#")
    return f"#{c}" if len(c) == 6 else default


def render_grid(data: dict, highlight_team: str = "McLaren") -> bytes:
    """Render a staggered two-column starting grid. Returns PNG bytes.

    ``data``: {"race", "round", "status" ("official"|"provisional"), "date",
               "entries": [{"pos", "code", "name", "team", "color", "note"}],
               "footnote"}
    """
    entries = data["entries"]
    W, side, gap = 1080, 48, 24
    col_w = (W - 2 * side - gap) // 2
    slot_h, row_h = 104, 120
    header_h, footer_h = 210, 150
    rows = (len(entries) + 1) // 2
    H = header_h + rows * row_h + row_h // 2 + footer_h

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Header
    d.rectangle([0, 0, W, 8], fill=PAPAYA)
    d.text((side, 40), data["race"].upper(), font=_font(_BOLD, 40), fill=TEXT)
    sub = f"Starting grid  ·  Round {data['round']}" + (f"  ·  {data['date']}" if data.get("date") else "")
    d.text((side, 98), sub, font=_font(_REG, 26), fill=MUTED)
    official = data["status"] == "official"
    chip = "OFFICIAL" if official else "PROVISIONAL"
    chip_font = _font(_BOLD, 22)
    cw = d.textlength(chip, font=chip_font) + 36
    d.rounded_rectangle([side, 146, side + cw, 186], radius=20, fill=GREEN if official else AMBER)
    d.text((side + 18, 153), chip, font=chip_font, fill="#111111")

    # Slots: odd positions on the left, even on the right half a row lower,
    # like the painted boxes on a real grid.
    num_font, code_font = _font(_BOLD, 44), _font(_BOLD, 34)
    name_font, note_font = _font(_REG, 22), _font(_BOLD, 20)
    for e in entries:
        idx = e["pos"] - 1
        col, row = idx % 2, idx // 2
        x = side + col * (col_w + gap)
        y = header_h + row * row_h + (row_h // 2 if col else 0)
        mine = highlight_team.lower() in str(e.get("team", "")).lower()
        d.rounded_rectangle([x, y, x + col_w, y + slot_h], radius=14,
                            fill=CARD_HI if mine else CARD,
                            outline=PAPAYA if mine else None, width=3 if mine else 0)
        d.rounded_rectangle([x, y, x + 12, y + slot_h], radius=6, fill=_hex(e.get("color")))
        d.line([x + 30, y + 10, x + 110, y + 10], fill="#ffffff", width=3)  # grid box marking
        d.text((x + 28, y + 26), str(e["pos"]), font=num_font, fill=TEXT)
        d.text((x + 112, y + 16), e["code"], font=code_font, fill=TEXT)
        team = e.get("team") or ""
        d.text((x + 112, y + 60), f"{e.get('name', '')}  ·  {team}", font=name_font, fill=MUTED)
        if e.get("note"):
            nw = d.textlength(e["note"], font=note_font)
            d.text((x + col_w - nw - 16, y + 22), e["note"], font=note_font, fill=AMBER)

    # Footer
    fy = H - footer_h + 30
    d.line([side, fy - 14, W - side, fy - 14], fill="#2a2f39", width=2)
    foot_font = _font(_REG, 20)
    for i, line in enumerate(_wrap(d, data.get("footnote", ""), foot_font, W - 2 * side)[:4]):
        d.text((side, fy + i * 28), line, font=foot_font, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _wrap(d: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if d.textlength(trial, font=font) <= width:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines
