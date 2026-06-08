import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import get_next_race_info, get_qualifying_results
from utils.groq_client import chat, SMART_MODEL
from utils.tavily_client import search, format_search_results
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply


async def run_fantasy(message, user_id: int) -> None:
    """Core /fantasy logic, reusable by the command and the race-weekend hub."""
    if is_rate_limited(user_id):
        await message.reply_text("Slow down — one question at a time.")
        return

    await message.reply_chat_action("typing")

    next_race, qual_data = await asyncio.gather(
        get_next_race_info(),
        asyncio.to_thread(get_qualifying_results),
    )
    race_name = next_race["name"] if next_race else "the next race"
    circuit = f"{next_race['location']}, {next_race['country']}" if next_race else ""
    qual_summary = ""
    if qual_data and "error" not in qual_data:
        qual_summary = "Qualifying grid:\n"
        for r in qual_data["results"][:10]:
            qual_summary += f"P{r['position']}: {r['driver']} ({r['team']})\n"

    grid_results, fantasy_results = await asyncio.gather(
        search("2026 F1 driver lineup all teams current season grid", max_results=4),
        search(f"F1 Fantasy 2026 best value drivers picks {race_name}", max_results=4),
    )
    def trim_results(results: list[dict], max_content: int = 200) -> list[dict]:
        return [{**r, "content": r.get("content", "")[:max_content]} for r in results]

    search_context = ""
    if grid_results:
        search_context += "Current 2026 F1 driver and constructor lineup:\n" + format_search_results(trim_results(grid_results)) + "\n"
    if fantasy_results:
        search_context += "F1 Fantasy form and value picks:\n" + format_search_results(trim_results(fantasy_results))

    prompt = f"""Give F1 Fantasy picks for {race_name} at {circuit}.

{qual_summary}

Recent F1 Fantasy context and form:
{search_context}

Give three picks:
1. Top pick: the safest, highest-ceiling choice (probably expensive, but worth it)
2. Value pick: someone priced lower who could outperform their cost this weekend
3. Constructor pick: best team to back for this circuit

For each pick, give a brief reason. Think about:
- Circuit characteristics and who tends to perform well here
- Current form and reliability
- Points scoring potential (fastest lap, positions gained, etc.)
- Price relative to likely return

Don't hedge everything. Make actual recommendations with actual reasoning."""

    response = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=SMART_MODEL,
    )

    await safe_reply(message, response)


async def fantasy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_fantasy(update.message, update.effective_user.id)
