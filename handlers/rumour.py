import re
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import ContextTypes
from utils.groq_client import chat, SMART_MODEL, MCLAREN_ANGLE
from utils.tavily_client import search
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import stream_reply


def _numbered(results: list[dict]) -> str:
    return "\n\n".join(
        f"[{i}] {r.get('title', '')} ({r.get('url', '')})\n{r.get('content', '')}"
        for i, r in enumerate(results, 1)
    )


def _sources(text: str, results: list[dict]) -> str:
    """Links for the sources the reply actually cites, in citation order."""
    cited = []
    for n in re.findall(r"\[(\d+)\]", text):
        i = int(n)
        if 1 <= i <= len(results) and i not in cited:
            cited.append(i)
    lines = []
    for i in cited:
        r = results[i - 1]
        title = re.sub(r"[*_`\[\]]", "", r.get("title") or urlparse(r.get("url", "")).netloc)
        lines.append(f"[{i}] [{title}]({r.get('url', '')})")
    return "\n\n*Sources*\n" + "\n".join(lines) if lines else ""


async def rumour_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down, one question at a time.")
        return

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
    search_context = _numbered(search_results) if search_results else "No recent results found."

    prompt = f"""The user wants the latest F1 rumours and news about: {topic}

Here is what recent F1 sources are reporting:
{search_context}

Give a rundown of what's being said. You must clearly distinguish between:
- What has been officially confirmed (by the team, driver, or FIA)
- What credible sources are reporting but hasn't been confirmed
- What is speculation, paddock gossip, or single-source rumour

Label these clearly within your response. Don't sensationalise things that are just rumours
and don't downplay things that have actually been confirmed.
If the search results don't give you much to work with, be honest about that rather than padding it out.
Cite the numbered sources you rely on with their number in square brackets, like [2], right after the claim."""

    async def produce(on_text):
        text = await chat(
            messages=[{"role": "user", "content": prompt + MCLAREN_ANGLE}],
            model=SMART_MODEL,
            on_text=on_text,
        )
        return text + _sources(text, search_results)

    await stream_reply(update.message, produce)
