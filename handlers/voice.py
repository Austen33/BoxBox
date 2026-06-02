import io
import aiohttp
from telegram import InputFile, Update
from telegram.ext import ContextTypes
from utils.groq_client import transcribe_audio, chat, synthesize_speech, SMART_MODEL
from utils.tavily_client import search, format_search_results
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply
from handlers.ask import LIVE_KEYWORDS


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

        transcript_lower = transcript.lower()
        needs_live_search = any(kw in transcript_lower for kw in LIVE_KEYWORDS)

        search_context = ""
        if needs_live_search:
            results = await search(f"F1 {transcript}", max_results=5)
            if results:
                search_context = f"\n\nRecent F1 source results:\n{format_search_results(results)}"

        prompt = f"""The user sent a voice note. Here is what they said:
"{transcript}"

{search_context}

Answer their F1 question or respond to what they said.
Use search results if they are relevant. Be honest if uncertain.
Keep the response concise — it will be spoken aloud as a voice note, so avoid bullet lists and markdown."""

        response = await chat(
            messages=[{"role": "user", "content": prompt}],
            model=SMART_MODEL,
        )

        await update.message.reply_chat_action("record_voice")

        try:
            tts_bytes = await synthesize_speech(response)
            voice_buf = io.BytesIO(tts_bytes)
            await update.message.reply_voice(
                voice=InputFile(voice_buf, filename="response.ogg"),
                caption=f'"{transcript}"',
            )
        except Exception:
            full_reply = f'_You said: "{transcript}"_\n\n{response}'
            await safe_reply(update.message, full_reply)

    except Exception:
        await update.message.reply_text(
            "Something went wrong processing your voice note. Try typing your question instead."
        )
