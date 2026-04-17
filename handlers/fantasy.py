import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import get_next_race_info, get_qualifying_results
from utils.groq_client import chat, FAST_MODEL
from utils.tavily_client import search, format_search_results


async def fantasy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_chat_action("typing")

    next_race = get_next_race_info()
    race_name = next_race["name"] if next_race else "the next race"
    circuit = f"{next_race['location']}, {next_race['country']}" if next_race else ""

    qual_data = get_qualifying_results()
    qual_summary = ""
    if qual_data and "error" not in qual_data:
        qual_summary = "Qualifying grid:\n"
        for r in qual_data["results"][:10]:
            qual_summary += f"P{r['position']}: {r['driver']} ({r['team']})\n"

    grid_results, fantasy_results = await asyncio.gather(
        search("2026 F1 driver lineup all teams current season grid", max_results=8),
        search(f"F1 Fantasy 2026 best value drivers picks {race_name}", max_results=8),
    )
    search_context = ""
    if grid_results:
        search_context += "Current 2026 F1 driver and constructor lineup:\n" + format_search_results(grid_results) + "\n"
    if fantasy_results:
        search_context += "F1 Fantasy form and value picks:\n" + format_search_results(fantasy_results)

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
        model=FAST_MODEL,
    )

    await update.message.reply_text(response, parse_mode="Markdown")
