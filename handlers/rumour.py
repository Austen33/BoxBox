from telegram import Update
from telegram.ext import ContextTypes
from utils.groq_client import chat, SMART_MODEL
from utils.tavily_client import search, format_search_results


async def rumour_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    topic = " ".join(context.args) if context.args else ""
    if not topic:
        await update.message.reply_text(
            "Tell me what you want the dirt on. Try /rumour Red Bull or /rumour Hamilton"
        )
        return

    await update.message.reply_chat_action("typing")

    search_results = await search(
        f"F1 {topic} rumour news transfer contract 2025 2026",
        max_results=6,
    )
    search_context = format_search_results(search_results) if search_results else "No recent results found."

    prompt = f"""The user wants the latest F1 rumours and news about: {topic}

Here is what recent F1 sources are reporting:
{search_context}

Give a rundown of what's being said. You must clearly distinguish between:
- What has been officially confirmed (by the team, driver, or FIA)
- What credible sources are reporting but hasn't been confirmed
- What is speculation, paddock gossip, or single-source rumour

Label these clearly within your response. Don't sensationalise things that are just rumours
and don't downplay things that have actually been confirmed.
If the search results don't give you much to work with, be honest about that rather than padding it out."""

    response = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=SMART_MODEL,
    )

    await update.message.reply_text(response, parse_mode="Markdown")
