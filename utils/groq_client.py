import asyncio
import base64
import json
import logging
import os
import re
import shutil

import httpx

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_client: httpx.AsyncClient | None = None


def _api_key() -> str:
    key = os.environ.get("OPEN_ROUTER_KEY")
    if not key:
        raise RuntimeError(
            "OPEN_ROUTER_KEY environment variable is not set. "
            "Add it to your .env file or environment."
        )
    return key


def _get_client() -> httpx.AsyncClient:
    """Lazily construct the HTTP client so missing env vars don't break import."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    return _client


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "X-Title": "BoxBox",
    }


async def _post(payload: dict, attempts: int = 3) -> dict:
    """POST a chat completion to OpenRouter, retrying on 429/5xx."""
    delay = 1.0
    for attempt in range(attempts):
        resp = await _get_client().post(OPENROUTER_URL, headers=_headers(), json=payload)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
            logger.warning("OpenRouter %s, retrying in %.0fs", resp.status_code, delay)
            await asyncio.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"OpenRouter error: {data['error']}")
        return data
    raise RuntimeError("OpenRouter request failed")


# Cheap and fast for short lookups; stronger model for reasoning-heavy answers.
FAST_MODEL = os.getenv("FAST_MODEL", "openai/gpt-6-luna")
SMART_MODEL = os.getenv("SMART_MODEL", "anthropic/claude-sonnet-5.5")
STT_MODEL = os.getenv("STT_MODEL", "google/gemini-3.5-flash-lite")
TTS_MODEL = os.getenv("TTS_MODEL", "openai/gpt-audio-mini")
# gpt-audio voices: alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar
TTS_VOICE = os.getenv("TTS_VOICE", "cedar")

# --- edge-tts (primary TTS) -----------------------------------------------
# Microsoft neural voices via Edge read-aloud. Free, no API key required.
# Default: en-GB-RyanNeural — calm, clear British male.
# Override via EDGE_TTS_VOICE env var. Full voice list: `edge-tts --list-voices`
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "en-GB-RyanNeural")

# Playback tempo multiplier for the edge-tts/gTTS fallbacks (ffmpeg atempo, pitch
# preserved). Range ~0.5-2.0.
try:
    TTS_SPEED = float(os.getenv("TTS_SPEED", "1.0"))
except ValueError:
    TTS_SPEED = 1.0
TTS_SPEED = max(0.5, min(TTS_SPEED, 2.0))  # atempo single-stage limits

_MARKDOWN_RE = re.compile(r"[*_`\[\]\\]")
# Orpheus paralinguistic cues (e.g. <laugh>, <sigh>). Orpheus performs these;
# other engines would read them literally, so strip them on the fallback path.
_EMOTION_TAG_RE = re.compile(r"<(?:laugh|chuckle|sigh|gasp|groan|yawn|cough|sniffle|sneeze)>", re.IGNORECASE)


def _strip_markdown(text: str) -> str:
    return _MARKDOWN_RE.sub("", text).strip()


def _strip_emotion_tags(text: str) -> str:
    return _EMOTION_TAG_RE.sub("", text).strip()


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.isfile(candidate):
            return candidate
    return None


async def _convert_to_ogg_opus(
    audio_bytes: bytes, input_format: str = "wav", speed: float = 1.0
) -> bytes:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg not found")
    args = [ffmpeg, "-f", input_format]
    if input_format == "s16le":  # raw PCM16 from gpt-audio: 24kHz mono
        args += ["-ar", "24000", "-ac", "1"]
    args += ["-i", "pipe:0"]
    if abs(speed - 1.0) > 0.01:
        args += ["-filter:a", f"atempo={speed:.3f}"]
    args += [
        "-c:a", "libopus", "-b:a", "64k", "-vbr", "on",
        "-f", "ogg", "pipe:1",
        "-loglevel", "error",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=audio_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode()}")
    return stdout


async def _convert_to_mp3(audio_bytes: bytes, input_format: str = "ogg") -> bytes:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg not found")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-f", input_format, "-i", "pipe:0",
        "-f", "mp3", "pipe:1", "-loglevel", "error",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=audio_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode()}")
    return stdout


async def _edge_tts_mp3(text: str) -> bytes:
    """Synthesize speech with Microsoft edge-tts. Free, no API key required."""
    import edge_tts
    text = _strip_emotion_tags(text)
    voice = os.getenv("EDGE_TTS_VOICE", EDGE_TTS_VOICE)
    communicate = edge_tts.Communicate(text, voice)
    audio = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return audio


async def _gtts_mp3(text: str) -> bytes:
    from gtts import gTTS
    import io as _io

    text = _strip_emotion_tags(text)

    def _run() -> bytes:
        tts = gTTS(text[:4096], lang="en")
        buf = _io.BytesIO()
        tts.write_to_fp(buf)
        return buf.getvalue()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


async def _openrouter_tts_pcm(text: str) -> bytes:
    """Speak `text` with an OpenRouter audio model. Returns raw 24kHz mono PCM16.

    Audio output requires streaming; the audio arrives as base64 PCM16 deltas.
    """
    payload = {
        "model": TTS_MODEL,
        "modalities": ["text", "audio"],
        "audio": {"voice": TTS_VOICE, "format": "pcm16"},
        "stream": True,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a text-to-speech engine. Read the user's message aloud "
                    "exactly as written, in a calm, natural British commentator tone. "
                    "Do not answer it, add to it or comment on it."
                ),
            },
            {"role": "user", "content": text},
        ],
    }
    pcm = bytearray()
    async with _get_client().stream(
        "POST", OPENROUTER_URL, headers=_headers(), json=payload
    ) as resp:
        if resp.status_code >= 400:
            body = (await resp.aread()).decode(errors="replace")
            raise RuntimeError(f"OpenRouter TTS {resp.status_code}: {body[:300]}")
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            for choice in chunk.get("choices", []):
                audio = (choice.get("delta") or {}).get("audio") or {}
                if audio.get("data"):
                    pcm += base64.b64decode(audio["data"])
    return bytes(pcm)


async def synthesize_speech(text: str) -> tuple[bytes, str]:
    """Returns (audio_bytes, fmt) where fmt is 'ogg' or 'mp3'.

    Engine priority: OpenRouter audio model -> edge-tts -> gTTS.
    """
    cleaned = _strip_markdown(_strip_emotion_tags(text))
    if len(cleaned) > 4096:
        cleaned = cleaned[:4096]

    source_bytes: bytes | None = None
    source_fmt = "mp3"
    convert_speed = 1.0

    # 1. OpenRouter audio model (raw PCM16 -> needs ffmpeg to be playable)
    if _find_ffmpeg():
        try:
            pcm = await _openrouter_tts_pcm(cleaned)
            if pcm:
                source_bytes, source_fmt = pcm, "s16le"
                logger.info("TTS: %s OK (%d bytes)", TTS_MODEL, len(pcm))
        except Exception:
            logger.warning("OpenRouter TTS failed, trying edge-tts", exc_info=True)

    # 2. edge-tts — free, no API key -> MP3
    if not source_bytes:
        convert_speed = TTS_SPEED
        try:
            source_bytes = await _edge_tts_mp3(cleaned)
            source_fmt = "mp3"
            logger.info("TTS: edge-tts OK (%d bytes)", len(source_bytes))
        except Exception:
            logger.warning("edge-tts failed, using gTTS fallback", exc_info=True)

    # 3. gTTS (last resort) -> MP3
    if not source_bytes:
        source_bytes = await _gtts_mp3(cleaned)
        source_fmt = "mp3"
        logger.info("TTS: gTTS fallback")

    # Convert to OGG/Opus if ffmpeg is available
    if _find_ffmpeg():
        try:
            ogg = await _convert_to_ogg_opus(
                source_bytes, input_format=source_fmt, speed=convert_speed
            )
            return ogg, "ogg"
        except Exception:
            logger.warning("OGG/Opus conversion failed", exc_info=True)

    # ffmpeg not available — only MP3 sources can be returned directly
    if source_fmt == "mp3":
        return source_bytes, "mp3"
    return await _gtts_mp3(cleaned), "mp3"

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
    data = await _post({
        "model": model,
        "messages": full_messages,
        "temperature": 0.7,
        "max_tokens": 1024,
    })
    return data["choices"][0]["message"]["content"] or ""


async def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe speech with an audio-input model via OpenRouter."""
    fmt = filename.rsplit(".", 1)[-1].lower()
    # OpenRouter's input_audio reliably accepts wav/mp3; convert Telegram's OGG/Opus.
    if fmt not in ("wav", "mp3") and _find_ffmpeg():
        try:
            audio_bytes = await _convert_to_mp3(audio_bytes, input_format=fmt)
            fmt = "mp3"
        except Exception:
            logger.warning("Audio conversion to mp3 failed, sending original", exc_info=True)
    data = await _post({
        "model": STT_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Transcribe this audio exactly. Output only the spoken words, "
                            "nothing else. If there is no speech, output nothing."
                        ),
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(audio_bytes).decode(),
                            "format": fmt,
                        },
                    },
                ],
            }
        ],
    })
    return (data["choices"][0]["message"]["content"] or "").strip()
