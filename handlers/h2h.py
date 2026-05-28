from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import get_driver_standings, get_current_season
from utils.groq_client import chat, FAST_MODEL
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply


def _find_driver(standings: dict, code_or_name: str) -> dict | None:
    needle = code_or_name.lower()
    for d in standings.get("drivers", []):
        if d.get("code", "").lower() == needle:
            return d
        if needle in d.get("driver", "").lower():
            return d
    return None


async def h2h_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /h2h [driver1] [driver2]\n"
            "Example: /h2h VER NOR"
        )
        return

    d1_input, d2_input = args[0], args[1]

    await update.message.reply_chat_action("typing")

    standings = await get_driver_standings()
    if not standings or "error" in standings:
        msg = standings.get("error", "unavailable") if standings else "unavailable"
        await update.message.reply_text(f"Couldn't load standings: {msg}")
        return

    d1 = _find_driver(standings, d1_input)
    d2 = _find_driver(standings, d2_input)

    if d1 is None or d2 is None:
        missing = []
        if d1 is None:
            missing.append(d1_input)
        if d2 is None:
            missing.append(d2_input)
        await update.message.reply_text(
            f"Couldn't find: {', '.join(missing)}. "
            f"Try a 3-letter code (e.g. VER, NOR) or the surname."
        )
        return

    year = get_current_season()
    points_diff = d1["points"] - d2["points"]
    leader = d1 if points_diff > 0 else d2 if points_diff < 0 else None

    data_text = (
        f"*Head-to-head — {year} season after R{standings['round']}*\n\n"
        f"{d1['driver']} ({d1['team']}): P{d1['position']} | {d1['points']} pts | {d1['wins']} wins\n"
        f"{d2['driver']} ({d2['team']}): P{d2['position']} | {d2['points']} pts | {d2['wins']} wins\n"
    )
    if leader is not None:
        data_text += f"Gap: {abs(points_diff):.0f} pts to {leader['driver'].split()[-1]}"
    else:
        data_text += "Gap: level on points"

    prompt = f"""{data_text}

Give a tight 2-sentence verdict on who has had the stronger season so far.
Use only the numbers above. No filler."""

    response = await chat(messages=[{"role": "user", "content": prompt}], model=FAST_MODEL)
    await safe_reply(update.message, f"{data_text}\n\n{response}")
