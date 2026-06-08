"""CallbackQuery router for the interactive race-weekend hub.

Inline buttons attached to /race carry ``hub:<action>`` callback data. This
handler answers the callback promptly (so Telegram doesn't show a spinner /
"query is too old" error) and dispatches to the same core logic the slash
commands use.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from handlers.predict import run_predict
from handlers.fantasy import run_fantasy
from handlers.standings import run_standings
from handlers.notify import subscribe_core

logger = logging.getLogger(__name__)


async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    # Acknowledge immediately so the button stops spinning.
    await query.answer()

    data = query.data or ""
    message = query.message
    user_id = query.from_user.id

    if data == "hub:predict":
        await run_predict(message, user_id)
    elif data == "hub:fantasy":
        await run_fantasy(message, user_id)
    elif data == "hub:standings":
        await run_standings(message, user_id)
    elif data == "hub:reminder":
        await subscribe_core(message, query.message.chat_id)
    else:
        logger.warning("Unknown hub callback data: %r", data)
