"""McLaren-focused commands: /teammates, /title, /pace, /debrief."""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from handlers.voice import send_voice_reply
from utils import mclaren
from utils.groq_client import chat, SMART_MODEL
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply

logger = logging.getLogger(__name__)

_RULES = (
    "Use only the numbers in the data. Do not invent any figures, causes or quotes. "
    "Be honest about McLaren's bad results as well as the good ones."
)


async def _guard(update: Update) -> bool:
    """Rate limit + typing indicator. Returns False if the request should stop."""
    if is_rate_limited(update.effective_user.id):
        await update.message.reply_text("Slow down, one question at a time.")
        return False
    await update.message.reply_chat_action("typing")
    return True


async def teammates_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    data = await mclaren.teammates_text()
    prompt = f"""{data}

Write a tight verdict on the McLaren team-mate battle in 3 to 5 sentences: who is ahead, by how much,
where the gap comes from (qualifying, race pace, reliability and bad luck count separately) and whether it is closing.
Treat both drivers fairly. {_RULES}"""
    verdict = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)
    await safe_reply(update.message, f"*Norris vs Piastri*\n\n{data}\n\n{verdict}")


async def title_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    data = await mclaren.title_text()
    prompt = f"""{data}

Explain McLaren's championship position in 4 to 6 sentences: what is realistically still possible in the drivers'
and constructors' titles, and what McLaren should be targeting from here. The "needed average" figures are
projections, say so. {_RULES}"""
    verdict = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL, effort="medium")
    await safe_reply(update.message, f"*Title maths*\n\n{data}\n\n{verdict}")


async def pace_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    from utils.pace import pace_report

    completed = sorted(await mclaren.get_completed_rounds())
    if not completed:
        await update.message.reply_text("No races completed yet, so there's no pace data.")
        return
    await update.message.reply_text("Crunching lap data. First run after a race can take a minute...")
    data = await asyncio.to_thread(pace_report, completed, 3)
    prompt = f"""{data}

Read this like a race engineer: is McLaren's race pace improving or getting worse relative to Mercedes, Ferrari
and Red Bull, and what does it say about the upgrades? 3 to 5 sentences. The trend covers only a few races
at different circuits, so don't overstate it. {_RULES}"""
    verdict = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL, effort="medium")
    await safe_reply(update.message, f"*Pace and upgrades*\n\n{data}\n\n{verdict}")


async def debrief_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    name, data = await mclaren.debrief_text()
    if not data:
        await update.message.reply_text("No race results to debrief yet.")
        return
    prompt = f"""{data}

Give a McLaren debrief of this race as a short spoken voice note, about 110 to 150 words. Cover how each McLaren
driver's weekend went (qualifying to finish), the points impact, and where it leaves McLaren in the championship.
Write it exactly how a person talks: short sentences, contractions, no bullet points, no markdown, say 'Formula One'
not 'F1' and spell out positions ('fifth', not 'P5'). {_RULES}"""
    script = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)
    await update.message.reply_chat_action("record_voice")
    sent = await send_voice_reply(update.message, script, caption=f"McLaren debrief: {name}")
    await safe_reply(update.message, script if sent else f"*McLaren debrief: {name}*\n\n{script}")
