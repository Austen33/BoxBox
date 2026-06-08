import asyncio
import re
from telegram import Update
from telegram.ext import ContextTypes
from utils.groq_client import chat, SMART_MODEL
from utils.tavily_client import search, format_search_results
from utils.f1_data import (
    get_last_race_results_async,
    get_driver_standings,
    get_constructor_standings,
    get_qualifying_results,
    get_current_season,
    resolve_round,
)
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply

STANDINGS_KEYWORDS = [
    "standings", "championship", "points", "who is leading", "who's leading",
    "results", "last race", "most recent race", "most recent", "recent race",
    "winner", "who won", "podium", "race result",
]

QUALI_KEYWORDS = [
    "quali", "qualifying", "qualified", "pole", "front row", "q3", "grid",
]

# Words that signal the user wants up-to-date / current-season information.
CURRENT_SIGNALS = [
    "latest", "recent", "now", "current", "today", "this week", "this season",
    "upcoming", "next race", "so far", "right now",
]

# Words that imply a live web search is genuinely needed (news, transfers,
# paddock talk). Deliberately excludes ultra-generic terms like
# "driver"/"team"/"best"/"season" that used to fire Tavily on pure-history
# questions ("who's the best driver ever").
LIVE_KEYWORDS = [
    "news", "rumour", "rumor", "update", "just",
    "announce", "announced", "signed", "confirmed", "breaking",
    "lineup", "transfer", "contract", "fantasy",
]

_RACE_RESULT_KEYWORDS = [
    "last race", "most recent race", "most recent", "recent race",
    "race result", "podium", "who won", "winner",
]


def _extract_years(query: str) -> list[int]:
    return [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", query)]


async def _fetch_qualifying(query: str, query_lower: str) -> dict | None:
    """Fetch real qualifying results, resolving a named circuit if the query
    contains one, otherwise the most recent completed qualifying session."""
    year_match = re.search(r"\b(19|20)\d{2}\b", query)
    year = int(year_match.group(0)) if year_match else get_current_season()

    round_number = await resolve_round(year, query)
    return await asyncio.to_thread(get_qualifying_results, year, round_number)


async def get_f1_response(query: str, for_voice: bool = False) -> str:
    query_lower = query.lower()
    current_year = get_current_season()
    years = _extract_years(query)
    mentions_past_year = any(y < current_year for y in years)
    mentions_current_year = any(y == current_year for y in years)
    has_current_signal = mentions_current_year or any(
        kw in query_lower for kw in CURRENT_SIGNALS
    )

    asks_standings = any(kw in query_lower for kw in STANDINGS_KEYWORDS)
    asks_quali = any(kw in query_lower for kw in QUALI_KEYWORDS)

    needs_standings_data = asks_standings
    needs_quali_data = asks_quali
    # Only reach for live web search when there's a genuine "currentness" signal
    # or an explicit live-news keyword — not for purely historical questions.
    needs_live_search = (
        any(kw in query_lower for kw in LIVE_KEYWORDS)
        or has_current_signal
        or (asks_standings and not mentions_past_year)
        or (asks_quali and not mentions_past_year)
    )

    # Standings/results tables are season-specific. If the user named a past
    # year, fetch that season's data so we never pass 2026 tables for a 2008
    # question. ``None`` means current season.
    standings_year = None
    if asks_standings and mentions_past_year:
        standings_year = max(y for y in years if y < current_year)

    f1_context = ""
    if needs_quali_data:
        qual_data = await _fetch_qualifying(query, query_lower)
        if qual_data and "error" not in qual_data:
            f1_context += f"Qualifying — {qual_data['name']} {qual_data['year']}:\n"
            for q in qual_data["results"]:
                q3 = q.get("q3", "").strip()
                time_part = f" - {q3}" if q3 and q3 not in ("nan", "NaT", "None") else ""
                f1_context += f"P{q['position']}: {q['driver']} ({q['team']}){time_part}\n"
            f1_context += "\n"

    if needs_standings_data:
        fetch_constructors = "constructor" in query_lower or "team" in query_lower
        if fetch_constructors:
            driver_data, constructor_data, race_data = await asyncio.gather(
                get_driver_standings(standings_year),
                get_constructor_standings(standings_year),
                get_last_race_results_async(standings_year),
            )
        else:
            driver_data, race_data = await asyncio.gather(
                get_driver_standings(standings_year),
                get_last_race_results_async(standings_year),
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

    race_result_query = any(kw in query_lower for kw in _RACE_RESULT_KEYWORDS)

    if for_voice:
        formatting_instruction = (
            "\nThis reply will be spoken aloud as a voice note, so write it exactly how a person talks, "
            "not how they write. Use short sentences, contractions, and a relaxed, natural rhythm with commas "
            "for breathing room. No bullet points, no markdown, no formatting symbols, no numbered lists. "
            "Say things the way you'd say them out loud: 'Formula One' not 'F1', 'first' or 'took the win' "
            "not 'P1', spell out numbers naturally. "
            "Where it genuinely fits the tone, you may include at most one or two emotion cues in angle brackets "
            "that the voice engine performs, chosen only from this exact set: <laugh> <chuckle> <sigh>. "
            "Use them sparingly and only when they match the moment — never force them."
        )
    else:
        formatting_instruction = (
            "\nPresent race results as a clean list (P1/P2/P3 etc.) with driver and team. "
            "Do not explain how you found the data or hedge about sources. Just give the result directly."
            if race_result_query and f1_context else ""
        )

    prompt = f"""The user is asking: {query}

{combined_context}

Answer this F1 question accurately. Prioritise the live F1 data over search results over training knowledge for current season info.
For current-season race or qualifying results, only state results that appear in the Live F1 data above. If the specific session or race the user asked about is not present in that data, say you don't have those results rather than guessing — never produce results from memory or infer them from news headlines.
For historical or technical questions, draw on your training knowledge.
Keep the answer concise and to the point. If you are not certain about something, say so.{formatting_instruction}"""

    return await chat(
        messages=[{"role": "user", "content": prompt}],
        model=SMART_MODEL,
    )


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
    response = await get_f1_response(query)
    await safe_reply(update.message, response)
