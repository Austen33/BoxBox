from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import resolve_round, get_race_rewind_data
from utils.groq_client import chat, SMART_MODEL
from utils.tavily_client import search, format_search_results
from utils.rate_limit import is_rate_limited


async def rewind_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /rewind [circuit] [year]\n"
            "Example: /rewind Bahrain 2024\n"
            "Example: /rewind Monaco 2023\n"
            "Relive the key moments, controversies, and turning points of any past race."
        )
        return

    year_str = args[-1]
    circuit_input = " ".join(args[:-1])

    try:
        year = int(year_str)
    except ValueError:
        await update.message.reply_text(
            f"'{year_str}' isn't a valid year. Usage: /rewind [circuit] [year]\n"
            "Example: /rewind Bahrain 2024"
        )
        return

    if year < 1950 or year > 2026:
        await update.message.reply_text("Year must be between 1950 and 2026.")
        return

    await update.message.reply_chat_action("typing")

    # Resolve circuit name to round number
    round_number = await resolve_round(year, circuit_input)

    if not round_number:
        # Fallback: search-based rewind
        search_results = await search(
            f"F1 {circuit_input} {year} race recap highlights controversies key moments",
            max_results=6,
        )
        if search_results:
            search_context = format_search_results(search_results)
            prompt = f"""Summarize the {year} {circuit_input} Grand Prix for a hardcore F1 fan.

Focus on:
- What happened at the start and how the race unfolded
- Key turning points: safety cars, penalties, crashes, strategy calls that changed the result
- Controversies or close battles
- The final result and who gained/lost the most

Search results:
{search_context}

Write it like you're telling a mate who missed the race what happened. No filler, no generic descriptions. Specific moments, specific laps, specific decisions. Keep it under 200 words."""
            response = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)
            await update.message.reply_text(f"*{circuit_input.title()} GP {year} — Rewind*\n\n{response}", parse_mode="Markdown")
            return

        await update.message.reply_text(
            f"Couldn't find a race at '{circuit_input}' in {year}. "
            f"Try names like Bahrain, Monaco, Silverstone, Spa, Monza."
        )
        return

    # Load race data from FastF1
    race_data = get_race_rewind_data(year, round_number)

    if race_data is None or (race_data and "error" in race_data):
        error_msg = race_data.get("error", "unknown") if race_data else "unknown"
        await update.message.reply_text(
            f"Couldn't load race data for {circuit_input} {year} ({error_msg}). "
            f"FastF1 may not have detailed data for this race yet."
        )
        return

    # Build data summary for the LLM
    race_name = race_data["name"]
    total_laps = race_data["total_laps"]

    results_text = "Results (Grid → Finish):\n"
    for f in race_data["finishers"]:
        grid = f["grid"]
        pos = f["position"]
        gain_loss = f"+{grid - pos}" if grid > pos else f"{grid - pos}" if grid < pos else "—"
        results_text += f"P{pos} {f['driver']} ({f['team']}) — started P{grid} ({gain_loss})\n"

    dnfs_text = ""
    if race_data["dnfs"]:
        dnfs_text = "DNFs:\n"
        for d in race_data["dnfs"]:
            dnfs_text += f"{d['driver']} ({d['team']}) — {d['status']} (started P{d['grid']})\n"

    events_text = ""
    if race_data["track_events"]:
        events_text = "Track events:\n" + "\n".join(f"  - {e}" for e in race_data["track_events"]) + "\n"

    rc_text = ""
    if race_data["rc_messages"]:
        rc_text = "Race control:\n" + "\n".join(f"  - {m}" for m in race_data["rc_messages"][:10]) + "\n"

    pit_text = ""
    if race_data["pit_summary"]:
        pit_text = "Pit windows:\n" + "\n".join(f"  - {p}" for p in race_data["pit_summary"]) + "\n"

    fl_text = race_data["fastest_lap"] + "\n" if race_data["fastest_lap"] else ""
    weather_text = race_data["weather"] + "\n" if race_data["weather"] else ""

    # Search for race report context
    search_results = await search(
        f"F1 {race_name} {year} race recap highlights controversy turning point",
        max_results=5,
    )
    search_context = format_search_results(search_results) if search_results else ""

    prompt = f"""You are writing a race rewind for the {race_name} {year} ({total_laps} laps).

Here is the hard data:

{results_text}
{dnfs_text}{events_text}{rc_text}{pit_text}{fl_text}{weather_text}

Additional context from F1 media:
{search_context}

Write a concise race rewind for a hardcore F1 fan. Rules:
- Open with the start: who got away well, who didn't
- Cover the key turning points: when did the race actually change? Safety cars, pit stop calls, penalties, incidents. Be specific about when things happened (lap numbers if available)
- Mention any controversies: close calls, penalties debated, team orders, steward decisions
- Close with who won and what it meant for the championship or the narrative
- No filler. No "it was an exciting race" or "fans were on the edge of their seats". Just what happened and why it mattered
- Under 250 words
- Write like you're telling a mate who missed it what happened, not like a Wikipedia article"""

    response = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)

    await update.message.reply_text(
        f"*{race_name} {year} — Rewind*\n\n{response}",
        parse_mode="Markdown",
    )
