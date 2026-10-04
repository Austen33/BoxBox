"""/grid, plus attaching the grid image to grid questions in chat, /ask and voice."""

import io
import logging
import re

from telegram import InputFile, Update
from telegram.ext import ContextTypes

from utils import grid, graphics
from utils.rate_limit import is_rate_limited

logger = logging.getLogger(__name__)

_GRID_RE = re.compile(
    r"\b(grid|starting order|start(?:ing)? position|line ?up for the start|where (?:does|do|will) \w+ start)\b",
    re.IGNORECASE,
)


def wants_grid(text: str) -> bool:
    """A question about this weekend's starting grid (not a past season)."""
    if not text or re.search(r"\b(?:19|20)\d{2}\b", text):
        return False
    return bool(_GRID_RE.search(text))


# Words people add to race names that the schedule doesn't use ("monaco gp").
_FILLER = {"gp", "grand", "prix", "the", "race", "grid", "of", "for"}


async def parse_event(args: list[str]) -> tuple[int | None, int | None, str]:
    """/grid arguments -> (year, round, error). (None, None, "") means this weekend.

    Accepts a race/circuit/country name, a year and/or a round number in any
    order: "monaco 2024", "2023 silverstone", "5", "2024 12".
    """
    from utils.f1_data import get_current_season, resolve_round

    season = get_current_season()
    year = rnd = None
    words = []
    for a in args:
        if re.fullmatch(r"(19[5-9]\d|20\d\d)", a) and int(a) <= season:
            year = int(a)
        elif a.isdigit() and 1 <= int(a) <= 30:
            rnd = int(a)
        elif a.lower() not in _FILLER:
            words.append(a)
    name = " ".join(words)
    if year is None and rnd is None and not name:
        return None, None, ""
    year = year or season
    if name:
        rnd = await resolve_round(year, name)
        if rnd is None:
            return None, None, f"Couldn't find a race matching '{name}' in {year}. Try e.g. /grid monaco 2024"
    elif rnd is None:
        return None, None, f"Which {year} race? Try e.g. /grid silverstone {year} or /grid {year} 5 (round number)"
    return year, rnd, ""


async def send_grid_image(message, year: int | None = None, rnd: int | None = None) -> bool:
    """Render and send a starting grid (this weekend's by default). False if there's no grid."""
    try:
        data = await grid.grid_data(year, rnd)
        if not data or not data.get("entries"):
            return False
        logos = await graphics.team_logos([e["team"] for e in data["entries"]], data["year"])
        png = graphics.render_grid(data, logos)
        label = "Official" if data["status"] == "official" else "Provisional"
        name = data["race"] if rnd is None else f"{data['year']} {data['race']}"
        await message.reply_photo(
            photo=InputFile(io.BytesIO(png), filename="grid.png"),
            caption=f"{label} starting grid: {name}",
        )
        return True
    except Exception:
        logger.exception("grid image failed")
        return False


async def grid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_rate_limited(update.effective_user.id):
        await update.message.reply_text("Slow down, one question at a time.")
        return
    year, rnd, error = await parse_event(context.args or [])
    if error:
        await update.message.reply_text(error)
        return
    await update.message.reply_chat_action("upload_photo")
    if not await send_grid_image(update.message, year, rnd):
        if rnd is None:
            await update.message.reply_text(
                "No grid yet for this weekend. It appears once qualifying has been run.\n"
                "For a past race, try e.g. /grid monaco 2024"
            )
        else:
            await update.message.reply_text(
                f"No grid for round {rnd} of {year} yet. It appears once qualifying has been run."
            )
