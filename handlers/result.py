from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import get_full_race_results_async
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply


def _is_finisher(result: dict) -> bool:
    """Ergast classifies finishers with a numeric positionText (lapped drivers
    included). Retirees/DSQ/DNS get letters (R, D, W, N, ...), so anything
    non-numeric is a non-finisher."""
    return result.get("position_text", "").isdigit()


async def result_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    await update.message.reply_chat_action("typing")

    data = await get_full_race_results_async()
    if not data or "error" in data:
        err = data.get("error", "unknown") if data else "unknown"
        await update.message.reply_text(f"Couldn't fetch race results: {err}")
        return

    race_name = data["name"]
    year = data["year"]
    results = data["results"]

    finishers = [r for r in results if _is_finisher(r)]
    dnfs = [r for r in results if not _is_finisher(r)]

    lines: list[str] = [f"*{race_name} {year}*\n"]

    for r in finishers:
        pos = r["position"]
        name = r["driver"]
        team = r["team"]
        gap = r["gap"]
        if pos == 1:
            lines.append(f"P1  {name} ({team})")
        else:
            suffix = f" +{gap}" if gap else f" {r['status']}"
            lines.append(f"P{pos:<2} {name} ({team}){suffix}")

    if dnfs:
        lines.append("")
        lines.append("DNF:")
        for r in dnfs:
            last = r["driver"].split()[-1]
            lines.append(f"{last} — {r['status']}")

    await safe_reply(update.message, "\n".join(lines))
