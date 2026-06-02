import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from utils.groq_client import chat, SMART_MODEL
from utils.tavily_client import search, format_search_results
from utils.f1_data import get_last_race_results, get_driver_standings, get_constructor_standings
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply

STANDINGS_KEYWORDS = [
    "standings", "championship", "points", "who is leading", "who's leading",
    "results", "last race", "most recent race", "most recent", "recent race",
    "winner", "who won", "podium", "race result",
]

LIVE_KEYWORDS = [
    "latest", "recent", "now", "current", "today", "this week", "this season",
    "2024", "2025", "2026", "news", "rumour", "rumor", "update", "just",
    "announce", "signed", "confirmed", "breaking",
    "driver", "drivers", "grid", "fantasy", "team", "worst", "best", "pick",
    "season", "lineup", "constructor",
]


async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("What do you want to know? Try /ask who has the most wins at Monaco")
        return

    await update.message.reply_chat_action("typing")

    query_lower = query.lower()
    needs_standings_data = any(kw in query_lower for kw in STANDINGS_KEYWORDS)
    needs_live_search = needs_standings_data or any(kw in query_lower for kw in LIVE_KEYWORDS)

    f1_context = ""
    if needs_standings_data:
        fetch_constructors = "constructor" in query_lower or "team" in query_lower
        if fetch_constructors:
            driver_data, constructor_data, race_data = await asyncio.gather(
                get_driver_standings(),
                get_constructor_standings(),
                asyncio.to_thread(get_last_race_results),
            )
        else:
            driver_data, race_data = await asyncio.gather(
                get_driver_standings(),
                asyncio.to_thread(get_last_race_results),
            )
            constructor_data = None

        if driver_data and "error" not in driver_data:
            f1_context += f"{driver_data['year']} Driver Championship Standings (after round {driver_data['round']}):\n"
            for d in driver_data["drivers"]:
                f1_context += f"P{d['position']}: {d['driver']} ({d['team']}) - {d['points']} pts, {d['wins']} wins\n"
            f1_context += "\n"

        if constructor_data and "error" not in constructor_data:
            f1_context += f"{constructor_data['year']} Constructor Standings (after round {constructor_data['round']}):\n"
            for c in constructor_data["constructors"]:
                f1_context += f"P{c['position']}: {c['team']} - {c['points']} pts\n"
            f1_context += "\n"

        if race_data and "error" not in race_data:
            f1_context += f"Last race: {race_data['name']} {race_data['year']}\n"
            for r in race_data["results"]:
                f1_context += f"P{int(r['position'])}: {r['driver']} ({r['team']})\n"
            f1_context += "\n"

    search_context = ""
    if needs_live_search:
        results = await search(f"F1 2026 {query}", max_results=8)
        if results:
            search_context = f"Recent information from F1 sources:\n{format_search_results(results)}"

    combined_context = ""
    if f1_context:
        combined_context += f"Live F1 data:\n{f1_context}\n"
    if search_context:
        combined_context += search_context

    race_result_query = any(kw in query_lower for kw in [
        "last race", "most recent race", "most recent", "recent race",
        "race result", "podium", "who won", "winner",
    ])

    formatting_instruction = (
        "\nPresent race results as a clean list (P1/P2/P3 etc.) with driver and team. "
        "Do not explain how you found the data or hedge about sources. Just give the result directly."
        if race_result_query and f1_context else ""
    )

    prompt = f"""The user is asking: {query}

{combined_context}

Answer this F1 question accurately. Prioritise the live F1 data over search results over training knowledge for current season info.
For historical or technical questions, draw on your training knowledge.
Keep the answer concise and to the point. If you are not certain about something, say so.{formatting_instruction}"""

    response = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=SMART_MODEL,
    )

    await safe_reply(update.message, response)
