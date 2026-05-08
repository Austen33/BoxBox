import aiohttp
from telegram import Update
from telegram.ext import ContextTypes
from utils.groq_client import transcribe_audio, chat, SMART_MODEL
from utils.tavily_client import search, format_search_results
from utils.rate_limit import is_rate_limited


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    voice = update.message.voice
    if not voice:
        return

    await update.message.reply_chat_action("typing")

    try:
        file = await context.bot.get_file(voice.file_id)
        async with aiohttp.ClientSession() as session:
            async with session.get(file.file_path) as resp:
                audio_bytes = await resp.read()

        transcript = await transcribe_audio(audio_bytes, filename="voice.ogg")

        if not transcript or not transcript.strip():
            await update.message.reply_text("Couldn't make out what you said. Try again?")
            return

        needs_live_search = any(word in transcript.lower() for word in [
            "latest", "recent", "now", "current", "today", "this week", "this season",
            "2024", "2025", "2026", "news", "rumour", "rumor", "update", "just",
            "announce", "signed", "confirmed", "breaking"
        ])

        search_context = ""
        if needs_live_search:
            results = await search(f"F1 {transcript}", max_results=5)
            if results:
                search_context = f"\n\nRecent F1 source results:\n{format_search_results(results)}"

        prompt = f"""The user sent a voice note. Here is what they said:
"{transcript}"

{search_context}

Answer their F1 question or respond to what they said.
Use search results if they are relevant. Be honest if uncertain."""

        response = await chat(
            messages=[{"role": "user", "content": prompt}],
            model=SMART_MODEL,
        )

        full_reply = f'_You said: "{transcript}"_\n\n{response}'
        await update.message.reply_text(full_reply, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(
            f"Something went wrong processing your voice note. Try typing your question instead."
        )
