"""Data-driven reply graphics (drawn with Pillow, never AI-generated).

Every name, position and colour on the image comes from the data passed in:
driver names and team colours from F1's timing feed, positions from the
official classification. Nothing is guessed, so nothing can be invented.

The grid is styled after F1's broadcast graphics: carbon background, square
cards with a chamfered corner, a team-colour stripe and fade, three-letter
driver codes and white team logos. Type is Titillium Web (SIL OFL, F1's pre-2018
typeface); F1's own font is proprietary.
"""

import asyncio
import io
import logging
import os
import tempfile
from datetime import date

import httpx
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts")
_REG = os.path.join(_FONT_DIR, "TitilliumWeb-Regular.ttf")
_SEMI = os.path.join(_FONT_DIR, "TitilliumWeb-SemiBold.ttf")
_BOLD = os.path.join(_FONT_DIR, "TitilliumWeb-Bold.ttf")
_BLACK = os.path.join(_FONT_DIR, "TitilliumWeb-Black.ttf")

BG = "#15151e"       # F1 carbon black
BG_BAND = "#1b1b26"
CARD = "#22222c"
TEXT = "#ffffff"
MUTED = "#a7a7b3"
LINE = "#2f2f3b"
F1_RED = "#e10600"
AMBER = "#ffb020"

S = 2  # supersampling: draw at 2x, downscale for smooth edges


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size * S)


def _hex(c: str | None, default: str = "#808080") -> str:
    if not c:
        return default
    c = c.strip().lstrip("#")
    return f"#{c}" if len(c) == 6 else default


def _rgb(c: str) -> tuple[int, int, int]:
    c = _hex(c).lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


# --------------------------------------------------------------------------
# Team logos (white versions from F1's media CDN, 2024 onwards)
# --------------------------------------------------------------------------

_LOGO_URL = ("https://media.formula1.com/image/upload/f_png/c_fit,h_128/q_auto/v1740000001/"
             "common/f1/{year}/{slug}/{year}{slug}logowhite.png")
_LOGO_DIR = os.path.join(tempfile.gettempdir(), "f1_team_logos")
_FIRST_LOGO_YEAR = 2024
_missing: set[str] = set()  # "year/slug" known not to exist


def _logo_slug(team: str, year: int) -> str | None:
    """F1 CDN slug for a team, only where the brand on the car matches the logo.
    Renamed teams (Toro Rosso, Alfa Romeo, Racing Point...) get none rather than
    a wrong one; pre-2018 seasons get none at all."""
    t = team.lower()
    if year < 2018:
        return None
    if "red bull" in t:
        return "redbullracing"
    if "racing bulls" in t or t in ("rb", "rb f1 team") or "visa cash app" in t:
        return "rb" if year == 2024 else "racingbulls" if year >= 2025 else None
    if "sauber" in t:
        return "kicksauber" if year in (2024, 2025) else None
    for key, slug, since in (("mclaren", "mclaren", 0), ("ferrari", "ferrari", 0),
                             ("mercedes", "mercedes", 0), ("williams", "williams", 0),
                             ("haas", "haasf1team", 0), ("alpine", "alpine", 2021),
                             ("aston martin", "astonmartin", 2021), ("audi", "audi", 2026),
                             ("cadillac", "cadillac", 2026)):
        if key in t:
            return slug if year >= since else None
    return None


async def _fetch_logo(client: httpx.AsyncClient, slug: str, year: int) -> Image.Image | None:
    # Older seasons borrow the earliest logo F1 publishes for that (unchanged) team.
    years = [max(year, _FIRST_LOGO_YEAR)] + [y for y in (_FIRST_LOGO_YEAR, date.today().year)
                                             if y != max(year, _FIRST_LOGO_YEAR)]
    for y in years:
        key = f"{y}/{slug}"
        path = os.path.join(_LOGO_DIR, f"{y}-{slug}.png")
        if os.path.exists(path):
            return Image.open(path).convert("RGBA")
        if key in _missing:
            continue
        try:
            r = await client.get(_LOGO_URL.format(year=y, slug=slug))
            if r.status_code == 200:
                img = Image.open(io.BytesIO(r.content)).convert("RGBA")
                if img.getchannel("A").getbbox():  # not a blank placeholder
                    os.makedirs(_LOGO_DIR, exist_ok=True)
                    img.save(path)
                    return img
            _missing.add(key)
        except Exception as e:
            logger.info("team logo %s unavailable (%s)", key, e)
    return None


async def team_logos(teams: list[str], year: int) -> dict[str, Image.Image]:
    """Team name -> white logo image, for the teams that have one. Cached on disk."""
    slugs = {t: _logo_slug(t, year) for t in set(teams) if t}
    wanted = {s for s in slugs.values() if s}
    if not wanted:
        return {}
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        found = dict(zip(wanted, await asyncio.gather(*[_fetch_logo(client, s, year) for s in wanted])))
    return {t: found[s] for t, s in slugs.items() if s and found.get(s)}


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------

def _tracked(d: ImageDraw.ImageDraw, xy, text: str, font, fill, tracking: float = 0.0) -> float:
    """Draw letter-spaced text; returns its width. tracking is in em."""
    x, y = xy
    extra = font.size * tracking
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + extra
    return x - xy[0] - extra


def _tracked_len(d, text: str, font, tracking: float = 0.0) -> float:
    return sum(d.textlength(ch, font=font) for ch in text) + font.size * tracking * max(len(text) - 1, 0)


def _nice_date(iso: str) -> str:
    try:
        dt = date.fromisoformat(iso)
        return f"{dt.day} {dt.strftime('%b %Y')}".upper()
    except (ValueError, TypeError):
        return ""


def _fade(w: int, h: int, color: str, strength: int = 90) -> Image.Image:
    """Team colour fading out left to right, like the broadcast name straps."""
    r, g, b = _rgb(color)
    grad = Image.new("L", (w, 1))
    grad.putdata([int(strength * max(0.0, 1 - i / (w * 0.65))) for i in range(w)])
    layer = Image.new("RGBA", (w, h), (r, g, b, 0))
    layer.putalpha(grad.resize((w, h)))
    return layer


# --------------------------------------------------------------------------
# Starting grid
# --------------------------------------------------------------------------

def render_grid(data: dict, logos: dict[str, Image.Image] | None = None) -> bytes:
    """Render a staggered two-column starting grid. Returns PNG bytes.

    ``data``: {"race", "round", "status" ("official"|"provisional"), "date",
               "entries": [{"pos", "code", "name", "team", "color", "note"}],
               "footnote"}
    ``logos``: optional team name -> white logo image (see team_logos).
    """
    logos = logos or {}
    entries = data["entries"]
    W, side, gap = 1080, 40, 28
    col_w = (W - 2 * side - gap) // 2
    slot_h, row_h, cut = 96, 112, 20
    header_h, footer_h = 236, 140
    rows = (len(entries) + 1) // 2
    H = header_h + rows * row_h + row_h // 2 + footer_h

    img = Image.new("RGBA", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)

    def px(*v):  # output px -> canvas px
        return [int(n * S) for n in v]

    # Header: a faint diagonal band for motion, then the title block.
    d.polygon(px(0, 0, W * 0.72, 0, W * 0.58, header_h - 20, 0, header_h - 20), fill=BG_BAND)
    d.polygon(px(side, 44, side + 14, 44, side + 6, 96, side - 8, 96), fill=F1_RED)
    title_font = _font(_BLACK, 46)
    title = data["race"].upper()
    while d.textlength(title, font=title_font) > (W - 2 * side - 30) * S and title_font.size > 28 * S:
        title_font = _font(_BLACK, title_font.size // S - 2)
    d.text(px(side + 26, 34), title, font=title_font, fill=TEXT)

    meta_bits = [f"ROUND {data['round']}"]
    if _nice_date(data.get("date")):
        meta_bits.append(_nice_date(data["date"]))
    _tracked(d, px(side + 26, 104), "   ·   ".join(meta_bits), _font(_SEMI, 21), MUTED, 0.08)

    tag_font = _font(_BOLD, 21)
    tag = "STARTING GRID"
    tw = _tracked_len(d, tag, tag_font, 0.1) / S
    d.polygon(px(side + 10, 150, side + 44 + tw, 150, side + 34 + tw, 188, side, 188), fill=F1_RED)
    _tracked(d, px(side + 22, 153), tag, tag_font, TEXT, 0.1)
    official = data["status"] == "official"
    status, colour = ("OFFICIAL", TEXT) if official else ("PROVISIONAL", AMBER)
    sx = side + 58 + tw
    sw = _tracked_len(d, status, tag_font, 0.1) / S
    d.polygon(px(sx + 10, 150, sx + sw + 34, 150, sx + sw + 24, 188, sx, 188), outline=colour, width=2 * S)
    _tracked(d, px(sx + 17, 153), status, tag_font, colour, 0.1)

    # Slots: odd positions on the left, even on the right half a row lower,
    # like the boxes painted on a real grid.
    num_font = _font(_BLACK, 44)
    code_font = _font(_BLACK, 34)
    team_font = _font(_SEMI, 16)
    for e in entries:
        idx = e["pos"] - 1
        col, row = idx % 2, idx // 2
        x = side + col * (col_w + gap)
        y = header_h + row * row_h + (row_h // 2 if col else 0)
        colour = _hex(e.get("color"), "#6b6b78")

        # Card with a chamfered corner, tinted by the team colour.
        shape = px(0, 0, col_w - cut, 0, col_w, cut, col_w, slot_h, 0, slot_h)
        tile = Image.new("RGBA", (col_w * S, slot_h * S), CARD)
        tile.alpha_composite(_fade(col_w * S, slot_h * S, colour))
        mask = Image.new("L", tile.size, 0)
        ImageDraw.Draw(mask).polygon(shape, fill=255)
        img.paste(tile, (x * S, y * S), mask)

        # Position number, then a slanted team-colour stripe.
        num = str(e["pos"])
        nw = d.textlength(num, font=num_font) / S
        d.text(px(x + 42 - nw / 2, y + 14), num, font=num_font, fill=TEXT)
        d.polygon(px(x + 90, y + 16, x + 98, y + 16, x + 92, y + slot_h - 16, x + 84, y + slot_h - 16),
                  fill=colour)

        # Logo on the right.
        logo = logos.get(e.get("team", ""))
        right = x + col_w - 22
        if logo:
            lg = logo.copy()
            lg.thumbnail((70 * S, 44 * S), Image.LANCZOS)
            lx, ly = right * S - lg.width, (y + slot_h / 2) * S - lg.height // 2
            img.alpha_composite(lg, (int(lx), int(ly)))
            right -= lg.width / S + 16

        # Three-letter code, like the timing tower.
        tx = x + 112
        _tracked(d, px(tx, y + 6), e.get("code", "").upper(), code_font, TEXT, 0.04)

        team_x = _tracked(d, px(tx, y + 58), (e.get("team") or "").upper(), team_font, MUTED, 0.08)
        if e.get("note"):
            _tracked(d, (tx * S + team_x + 14 * S, (y + 58) * S), e["note"], team_font, AMBER, 0.06)

    # Footer
    fy = H - footer_h + 34
    d.line(px(side, fy - 16, W - side, fy - 16), fill=LINE, width=2 * S)
    foot_font = _font(_REG, 19)
    for i, line in enumerate(_wrap(d, data.get("footnote", ""), foot_font, (W - 2 * side) * S)[:4]):
        d.text(px(side, fy + i * 26), line, font=foot_font, fill=MUTED)

    out = img.resize((W, H), Image.LANCZOS).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
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
