import asyncio
import logging
import os
import re
import shutil
from groq import AsyncGroq

logger = logging.getLogger(__name__)

_client: AsyncGroq | None = None


def _get_client() -> AsyncGroq:
    """Lazily construct the Groq client so missing env vars don't break import."""
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY environment variable is not set. "
                "Add it to your .env file or environment."
            )
        _client = AsyncGroq(api_key=api_key)
    return _client

FAST_MODEL = "llama-3.1-8b-instant"
SMART_MODEL = "llama-3.3-70b-versatile"
WHISPER_MODEL = "whisper-large-v3-turbo"
TTS_MODEL = "canopylabs/orpheus-v1-english"
TTS_VOICE = "tara"

_MARKDOWN_RE = re.compile(r"[*_`\[\]\\]")


def _strip_markdown(text: str) -> str:
    return _MARKDOWN_RE.sub("", text).strip()


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.isfile(candidate):
            return candidate
    return None


async def _convert_to_ogg_opus(audio_bytes: bytes, input_format: str = "wav") -> bytes:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg not found")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-f", input_format, "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "64k", "-vbr", "on",
        "-f", "ogg", "pipe:1",
        "-loglevel", "error",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=audio_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode()}")
    return stdout


async def _gtts_mp3(text: str) -> bytes:
    from gtts import gTTS
    import io as _io

    def _run() -> bytes:
        tts = gTTS(text[:4096], lang="en")
        buf = _io.BytesIO()
        tts.write_to_fp(buf)
        return buf.getvalue()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


async def synthesize_speech(text: str) -> tuple[bytes, str]:
    """Returns (audio_bytes, fmt) where fmt is 'ogg' or 'mp3'."""
    cleaned = _strip_markdown(text)
    if len(cleaned) > 4096:
        cleaned = cleaned[:4096]

    # Try Groq TTS first (requires Orpheus terms acceptance)
    groq_wav: bytes | None = None
    try:
        response = await _get_client().audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=cleaned,
            response_format="wav",
        )
        wav = await response.read()
        if wav:
            groq_wav = wav
    except Exception:
        logger.warning("Groq TTS unavailable, using gTTS fallback", exc_info=True)

    source_bytes = groq_wav
    source_fmt = "wav"
    if source_bytes is None:
        source_bytes = await _gtts_mp3(cleaned)
        source_fmt = "mp3"

    # Convert to OGG/Opus if ffmpeg is available
    if _find_ffmpeg():
        try:
            ogg = await _convert_to_ogg_opus(source_bytes, input_format=source_fmt)
            return ogg, "ogg"
        except Exception:
            logger.warning("OGG/Opus conversion failed", exc_info=True)

    # ffmpeg not available — return MP3 directly (caller uses reply_audio)
    if source_fmt == "mp3":
        return source_bytes, "mp3"
    # Had WAV from Groq but no ffmpeg — fall back to gTTS MP3
    mp3 = await _gtts_mp3(cleaned)
    return mp3, "mp3"

SYSTEM_PROMPT = """You are BoxBox, a Telegram F1 bot. You are a knowledgeable mate who follows F1 obsessively.

Rules for every response:
- Write in plain, natural English. No textbook tone, no news article style.
- Never use em dashes as punctuation.
- Never use phrases like "it is worth noting", "dive into", "certainly", "delve", "it is important to note", "fascinatingly", "it's worth mentioning", "needless to say","genuinely".
- Avoid unnecessary bullet lists. Use prose unless a list genuinely helps the reader.
- Technical explanations should feel like a race engineer talking to a smart fan who wants to actually understand something, not just get a surface level answer.
- Always be factual. If something is uncertain, say so clearly.
- Keep responses concise but complete. Do not pad answers with filler sentences.
- Format for Telegram: use *bold* and _italic_ sparingly where it genuinely helps, keep paragraphs short.
- Never recommend drivers or teams based on memory alone. Always treat driver and constructor information as potentially outdated and rely on the search context provided.
- The current year is 2026. Always refer to the 2026 F1 season. If search results mention 2025, treat that as last season's data and flag it as such rather than presenting it as current."""


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _trim_messages_to_limit(messages: list, token_limit: int = 8000) -> list:
    total = sum(_estimate_tokens(m["content"]) for m in messages)
    if total <= token_limit:
        return messages

    # Preserve system prompt (index 0) and last user message (index -1)
    if len(messages) <= 2:
        return messages

    system_msg = messages[0]
    user_msg = messages[-1]
    reserved = _estimate_tokens(system_msg["content"]) + _estimate_tokens(user_msg["content"])
    budget = token_limit - reserved

    # Truncate the context message (middle messages or the user content if single-message)
    middle = messages[1:-1]
    trimmed = []
    for msg in middle:
        content = msg["content"]
        allowed_chars = budget * 4
        if allowed_chars <= 0:
            break
        trimmed.append({**msg, "content": content[:allowed_chars]})
        budget -= _estimate_tokens(content[:allowed_chars])

    return [system_msg] + trimmed + [user_msg]


async def chat(messages: list, model: str = SMART_MODEL, system: str = SYSTEM_PROMPT) -> str:
    full_messages = [{"role": "system", "content": system}] + messages
    full_messages = _trim_messages_to_limit(full_messages)
    response = await _get_client().chat.completions.create(
        model=model,
        messages=full_messages,
        temperature=0.7,
        max_tokens=1024,
    )
    return response.choices[0].message.content


async def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    transcription = await _get_client().audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=WHISPER_MODEL,
        response_format="text",
    )
    return transcription
