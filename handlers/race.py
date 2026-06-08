from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from utils.f1_data import get_next_race_info
from utils.groq_client import chat, FAST_MODEL
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply


def _hub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔮 Predict", callback_data="hub:predict"),
            InlineKeyboardButton("🎯 Fantasy", callback_data="hub:fantasy"),
        ],
        [
            InlineKeyboardButton("🏆 Standings", callback_data="hub:standings"),
            InlineKeyboardButton("⏰ Add reminder", callback_data="hub:reminder"),
        ],
    ])


async def race_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    await update.message.reply_chat_action("typing")

    race_info = await get_next_race_info()

    if race_info is None:
        await update.message.reply_text(
            "Can't find the next race right now. The season might be over or the schedule hasn't been published yet."
        )
        return

    sessions_text = ""
    for session_name, session_time in race_info["sessions"].items():
        sessions_text += f"- {session_name}: {session_time} Irish time\n"

    data_summary = f"""Next race: {race_info['name']}
Location: {race_info['location']}, {race_info['country']}
Round: {race_info['round']}
Countdown: {race_info['countdown']} until lights out

Session times (Irish time / Europe/Dublin):
{sessions_text}"""

    prompt = f"""Here is the data for the next F1 race weekend:

{data_summary}

Write a short, punchy race weekend preview message for the BoxBox Telegram bot.
Include the countdown, all session times, and the location.
Do NOT add any commentary, predictions, or filler about the circuit or what to watch for.
Just give the race name, countdown, and session times. Nothing else."""

    response = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=FAST_MODEL,
    )

    await safe_reply(update.message, response, reply_markup=_hub_keyboard())
