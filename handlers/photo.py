import logging

import aiohttp
from telegram import Update
from telegram.ext import ContextTypes

from utils import convo
from utils.groq_client import chat_vision
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply

logger = logging.getLogger(__name__)

_DEFAULT_QUESTION = "What's going on in this image, and what does it mean for the race or for McLaren?"

_PROMPT = """The user sent an image (a screenshot, photo, timing screen, graphic or social post) with this message: {question}

Describe only what you can actually see, then answer. Read any text, numbers, timings and team colours carefully
and say if something is too small or unclear to read. Do not guess driver identities from faces; use captions,
numbers, names and team liveries visible in the image. For anything about current results or standings that is
not visible in the image, rely on the live data you were given and say so. Keep it concise.
If the image and question have nothing to do with Formula 1 or motorsport, don't describe or answer it;
reply in one sentence that you're an F1 bot and only answer F1 questions."""


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.photo:
        return
    if is_rate_limited(update.effective_user.id):
        await msg.reply_text("Slow down — one question at a time.")
        return

    await msg.reply_chat_action("typing")
    try:
        file = await context.bot.get_file(msg.photo[-1].file_id)  # largest size
        async with aiohttp.ClientSession() as session:
            async with session.get(file.file_path) as resp:
                image = await resp.read()

        question = (msg.caption or "").strip() or _DEFAULT_QUESTION
        chat_id = update.effective_chat.id
        response = await chat_vision(
            _PROMPT.format(question=question), image, "image/jpeg",
            history=convo.get_history(chat_id),
        )
        convo.add_exchange(chat_id, f"[sent an image] {question}", response)
        await safe_reply(msg, response)
    except Exception:
        logger.exception("Photo handler error")
        await msg.reply_text("Couldn't read that image. Try sending it again, or ask in text.")
