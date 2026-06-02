import io
import logging
import aiohttp
from telegram import InputFile, Update
from telegram.ext import ContextTypes
from utils.groq_client import transcribe_audio, synthesize_speech
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply
from handlers.ask import get_f1_response

logger = logging.getLogger(__name__)


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

        response = await get_f1_response(transcript, for_voice=True)

        await update.message.reply_chat_action("record_voice")

        try:
            tts_bytes = await synthesize_speech(response)
            voice_buf = io.BytesIO(tts_bytes)
            await update.message.reply_voice(
                voice=InputFile(voice_buf, filename="response.ogg"),
                caption=f'"{transcript}"',
            )
        except Exception:
            logger.exception("TTS failed, falling back to text reply")
            full_reply = f'_You said: "{transcript}"_\n\n{response}'
            await safe_reply(update.message, full_reply)

    except Exception:
        logger.exception("Voice handler error")
        await update.message.reply_text(
            "Something went wrong processing your voice note. Try typing your question instead."
        )
