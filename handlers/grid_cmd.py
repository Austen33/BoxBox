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


async def send_grid_image(message) -> bool:
    """Render and send the current weekend's grid. False if there's no grid yet."""
    try:
        data = await grid.grid_data()
        if not data or not data.get("entries"):
            return False
        png = graphics.render_grid(data)
        label = "Official" if data["status"] == "official" else "Provisional"
        await message.reply_photo(
            photo=InputFile(io.BytesIO(png), filename="grid.png"),
            caption=f"{label} starting grid: {data['race']}",
        )
        return True
    except Exception:
        logger.exception("grid image failed")
        return False


async def grid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_rate_limited(update.effective_user.id):
        await update.message.reply_text("Slow down, one question at a time.")
        return
    await update.message.reply_chat_action("upload_photo")
    if not await send_grid_image(update.message):
        await update.message.reply_text(
            "No grid yet for this weekend. It appears once qualifying has been run."
        )
