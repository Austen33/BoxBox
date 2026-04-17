from telegram import Update
from telegram.ext import ContextTypes
from utils.groq_client import chat, SMART_MODEL
from utils.tavily_client import search, format_search_results


async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("What do you want to know? Try /ask who has the most wins at Monaco")
        return

    await update.message.reply_chat_action("typing")

    needs_live_search = any(word in query.lower() for word in [
        "latest", "recent", "now", "current", "today", "this week", "this season",
        "2024", "2025", "2026", "news", "rumour", "rumor", "update", "just",
        "announce", "signed", "confirmed", "breaking",
        "driver", "drivers", "grid", "fantasy", "team", "worst", "best", "pick",
        "season", "lineup", "constructor",
    ])

    search_context = ""
    if needs_live_search:
        results = await search(f"F1 2026 {query}", max_results=8)
        if results:
            search_context = f"\n\nHere is recent information from F1 sources:\n{format_search_results(results)}"

    prompt = f"""The user is asking: {query}

{search_context}

Answer this F1 question accurately. Use the search results if they are relevant and recent.
For historical or technical questions, draw on your training knowledge.
If you are not certain about something, say so rather than guessing."""

    response = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=SMART_MODEL,
    )

    await update.message.reply_text(response, parse_mode="Markdown")
