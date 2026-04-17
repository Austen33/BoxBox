from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import get_next_race_info
from utils.groq_client import chat, FAST_MODEL


async def race_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_chat_action("typing")

    race_info = get_next_race_info()

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
Add one or two sentences about what to watch for at this circuit or this weekend,
drawing on your knowledge of this race venue. Keep it tight, no waffle."""

    response = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=FAST_MODEL,
    )

    await update.message.reply_text(response, parse_mode="Markdown")
