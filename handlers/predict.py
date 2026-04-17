from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import get_qualifying_results, get_next_race_info
from utils.groq_client import chat, SMART_MODEL
from utils.tavily_client import search, format_search_results


async def predict_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_chat_action("typing")

    qual_data = get_qualifying_results()
    next_race = get_next_race_info()

    race_name = "the upcoming race"
    circuit_name = ""
    if next_race:
        race_name = next_race["name"]
        circuit_name = f"{next_race['location']}, {next_race['country']}"

    qual_summary = ""
    if qual_data and "error" not in qual_data:
        race_name = qual_data["name"]
        qual_summary = f"Qualifying results for {qual_data['name']} (Round {qual_data['round']}, {qual_data['year']}):\n"
        for r in qual_data["results"][:10]:
            qual_summary += f"P{r['position']}: {r['driver']} ({r['team']})\n"
    else:
        qual_summary = "Qualifying results not yet available."

    search_results = await search(f"F1 {race_name} race prediction form 2025", max_results=5)
    search_context = format_search_results(search_results) if search_results else ""

    prompt = f"""You are giving a pre-race winner prediction for {race_name} at {circuit_name}.

Qualifying data:
{qual_summary}

Recent news and context:
{search_context}

Give a prediction for the race winner and podium. Consider:
- Grid positions and who has pace
- Circuit characteristics and what suits which car
- Recent driver form and team momentum
- Any relevant news (weather, penalties, setup issues)

Be analytical but not dry. Explain your reasoning like you're talking to someone who watches every race.
Don't just say "pole sitter will win" - actually think about whether the race tends to mix things up here,
whether anyone behind has the pace to challenge, whether there's a wildcard.
Be honest if it's hard to call."""

    response = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=SMART_MODEL,
    )

    await update.message.reply_text(response, parse_mode="Markdown")
