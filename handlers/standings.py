import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import get_driver_standings, get_constructor_standings
from utils.rate_limit import is_rate_limited


async def standings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    await update.message.reply_chat_action("typing")

    drivers, constructors = await asyncio.gather(
        get_driver_standings(),
        get_constructor_standings(),
    )

    lines: list[str] = []

    if drivers and "error" not in drivers:
        lines.append(f"*Drivers Championship* (after round {drivers['round']})")
        for d in drivers["drivers"][:5]:
            lines.append(
                f"P{d['position']}: {d['driver']} ({d['team']}) — {d['points']} pts, {d['wins']} wins"
            )
    else:
        msg = drivers.get("error", "unavailable") if drivers else "unavailable"
        lines.append(f"Drivers standings unavailable: {msg}")

    lines.append("")

    if constructors and "error" not in constructors:
        lines.append(f"*Constructors Championship* (after round {constructors['round']})")
        for c in constructors["constructors"][:5]:
            lines.append(f"P{c['position']}: {c['team']} — {c['points']} pts")
    else:
        msg = constructors.get("error", "unavailable") if constructors else "unavailable"
        lines.append(f"Constructors standings unavailable: {msg}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
