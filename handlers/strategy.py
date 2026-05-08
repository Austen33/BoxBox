from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import get_lap_data_for_strategy
from utils.groq_client import chat, SMART_MODEL
from utils.tavily_client import search, format_search_results
from utils.rate_limit import is_rate_limited


async def strategy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    await update.message.reply_chat_action("typing")

    strategy_data = get_lap_data_for_strategy()

    if strategy_data is None or "error" in strategy_data:
        error_msg = strategy_data.get("error", "unknown") if strategy_data else "unknown"
        await update.message.reply_text(
            f"Couldn't pull strategy data right now. Either the race data isn't loaded yet or something went wrong ({error_msg}). Try again in a bit."
        )
        return

    sorted_drivers = sorted(
        strategy_data["strategies"].items(),
        key=lambda x: (x[1]["position"] is None, x[1]["position"] if x[1]["position"] is not None else 999)
    )

    strategy_summary = f"Race: {strategy_data['name']} ({strategy_data['year']}), {strategy_data['total_laps']} laps\n\n"
    strategy_summary += "Driver strategies:\n"

    for abbr, driver in sorted_drivers[:20]:
        stint_desc = " -> ".join(
            f"{s['compound']} ({s['laps']} laps)" for s in driver["stints"]
        )
        pos = driver["position"] if driver["position"] else "?"
        strategy_summary += f"P{pos} {driver['name']} ({driver['team']}): {stint_desc}\n"

    search_results = await search(f"F1 {strategy_data['name']} {strategy_data['year']} race strategy analysis", max_results=4)
    search_context = format_search_results(search_results) if search_results else ""

    prompt = f"""You are debriefing the tyre strategy from {strategy_data['name']} {strategy_data['year']}.

Here is the raw strategy data:
{strategy_summary}

Additional context from F1 sources:
{search_context}

Give a proper strategy breakdown like a race engineer debriefing a smart fan:
1. What each of the top teams did and why it made sense given their grid position
2. Who called it right and who got it wrong
3. What the optimal strategy actually was in hindsight
4. Any interesting undercuts, overcuts, or gambles that worked or backfired

Don't just list what everyone did. Explain the decision-making, the track position logic,
the tyre delta, when the safety car or VSC changed things if it did.
Make this feel like proper analysis, not a Wikipedia summary."""

    response = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=SMART_MODEL,
    )

    await update.message.reply_text(response, parse_mode="Markdown")
