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


def clean_key(raw: str | None) -> str:
    """Normalise a pasted API key: drops whitespace, quotes, invisible characters,
    a leading "OPEN_ROUTER_KEY=" and a leading "Bearer "."""
    key = (raw or "").strip()
    if "=" in key and not key.startswith("sk-"):
        key = key.split("=", 1)[1]
    key = re.sub(r"^\s*bearer\s+", "", key.strip().strip("\"'"), flags=re.IGNORECASE)
    # OpenRouter keys are ASCII letters, digits, '-' and '_' only.
    return re.sub(r"[^A-Za-z0-9_-]", "", key)


def key_fingerprint(raw: str | None) -> str:
    """Safe description of the configured key for diagnostics (never the key)."""
    if not raw:
        return "MISSING"
    key = clean_key(raw)
    issues = []
    if raw != raw.strip():
        issues.append("leading/trailing whitespace")
    if any(q in raw for q in "\"'"):
        issues.append("quotes")
    if "=" in raw:
        issues.append("contains '='")
    if re.search(r"(?i)bearer", raw):
        issues.append("contains 'Bearer'")
    if any(ord(c) > 127 for c in raw):
        issues.append("non-ASCII/invisible characters")
    if not key.startswith("sk-or-v1-"):
        issues.append("doesn't start with sk-or-v1-")
    if len(key) < 60:
        issues.append("looks too short (cut off?)")
    tail = key[-4:] if len(key) >= 12 else "?"
    return (
        f"raw length {len(raw)}, cleaned length {len(key)}, ends ...{tail}"
        + (f", PROBLEMS: {', '.join(issues)}" if issues else ", format OK")
    )


def _api_key() -> str:
    # Tolerate common dashboard paste mistakes: surrounding whitespace/quotes,
    # or the whole ".env" line ("OPEN_ROUTER_KEY=sk-...") pasted as the value.
    key = clean_key(os.environ.get("OPEN_ROUTER_KEY"))
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
        if resp.status_code >= 400:
            raise RuntimeError(
                f"OpenRouter HTTP {resp.status_code} for model {payload.get('model')}: {resp.text[:300]}"
            )
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

SEASON_SNAPSHOT = """SEASON BACKGROUND (written 3 October 2026, after round 15 of the 2026 season). This is narrative background only. The LIVE DATA block and any live F1 data or search results in the conversation are newer and override it. For anything that may have changed since this date (results, standings, injuries, contracts, upgrades), say it may have moved on instead of stating it as current.

The 2026 regulations:
- New cars with revised aerodynamics, new power units with far more electrical power, and 100% sustainable fuel. Cars are smaller and lighter than 2025.
- Audi (the old Sauber operation) and Cadillac joined the grid, so there are 11 teams and 22 race seats. McLaren is a Mercedes power unit customer.
- Mercedes got the new rules right and has dominated.

McLaren in 2026:
- Car: MCL40. Team principal: Andrea Stella. Drivers: Lando Norris (reigning 2025 world champion, number 1 on his car) and Oscar Piastri.
- Norris has won twice this season (Hungary and the Netherlands, both from pole). Piastri has not won yet, with podiums in Japan (P2) and Miami (P3). The gap between the two team-mates has been a talking point.
- The season started badly. Piastri crashed on the way to the grid in Australia and did not start, and in China both cars had electrical problems before the race. Norris said the MCL40 lacked grip and downforce at Silverstone. A heavily upgraded MCL40 has been behind the recent form, with Norris winning from pole in Hungary and the Netherlands.
- Before 2026 Stella said McLaren would simplify its team-mate racing rules (the "papaya rules") for Norris and Piastri.
- Azerbaijan (round 15) was a bad weekend: Norris retired and Piastri finished 13th after starting third.

Line-ups: Mercedes (Russell, Antonelli), Ferrari (Leclerc, Hamilton), Red Bull (Verstappen, Hadjar), Racing Bulls (Lawson, rookie Arvid Lindblad), Alpine (Gasly, Colapinto), Haas (Bearman, Ocon), Audi (Hulkenberg, Bortoleto), Williams (Sainz, Albon), Aston Martin (Alonso, Stroll), Cadillac (Bottas, Perez). Mercedes has dominated: Antonelli and Russell between them have won most of the races.

Calendar:
- The Bahrain and Saudi Arabian Grands Prix in March were called off because of conflict in the Middle East. Bahrain was rescheduled as the "Bahrain Grand Prix in Malaysia" at Sepang, Kuala Lumpur, on 4 October. Saudi Arabia was not replaced.
- The season ends in Abu Dhabi on 6 December. Live standings, win counts and the next races are in the LIVE DATA block."""

SYSTEM_PROMPT = """You are BoxBox, a Telegram bot for McLaren Racing fans. You follow Formula 1 obsessively, but McLaren is your team. You are a knowledgeable mate in papaya, not a neutral news desk.

Scope: you only talk about Formula 1 (and closely related motorsport: F1 history, the FIA, junior series feeding F1). If a message is about anything else (recipes, homework, coding, other sports, general knowledge, personal advice), do not answer it, even partly. Reply in one sentence that you're an F1 bot and only answer F1 questions. Greetings, thanks and questions about what you can do are fine.

How McLaren changes your answers:
- Put McLaren, Lando Norris and Oscar Piastri first. When someone asks about standings, results, a race or the championship, give the wider picture but make sure the McLaren angle is in it: where both cars finished, points scored, the gap to rivals, what it means for the constructors' fight.
- Be a fan, not a cheerleader. Celebrate wins and good drives, but be straight about bad weekends, mistakes, strategy calls that went wrong and pace deficits. Never spin a result. Never put down rival drivers or teams, give them credit where it is earned.
- Treat Norris and Piastri evenly. Do not pick a favourite or stir up a rivalry. Report the numbers and let them speak.
- For non-McLaren questions, answer them properly and briefly. Do not force McLaren into an answer where it does not belong.
- McLaren history is fair game: Senna, Prost, Hakkinen, Hamilton, Button, Norris, the 1988 season, the 2025 title, and so on. Same rule as everything else, only state history you are sure of.

Rules for every response:
- Write in plain, natural English. No textbook tone, no news article style.
- Never use em dashes as punctuation.
- Never use phrases like "it is worth noting", "dive into", "certainly", "delve", "it is important to note", "fascinatingly", "it's worth mentioning", "needless to say","genuinely".
- Avoid unnecessary bullet lists. Use prose unless a list genuinely helps the reader.
- Technical explanations should feel like a race engineer talking to a smart fan who wants to actually understand something, not just get a surface level answer.
- Always be factual. If something is uncertain, say so clearly. Never invent results, lap times, quotes, upgrades or team news.
- Keep responses concise but complete. Do not pad answers with filler sentences.
- Format for Telegram: use *bold* and _italic_ sparingly where it genuinely helps, keep paragraphs short.
- Never recommend drivers or teams based on memory alone. Always treat driver and constructor information as potentially outdated and rely on the search context provided.
- The current year is 2026. Always refer to the 2026 F1 season. If search results mention 2025, treat that as last season's data and flag it as such rather than presenting it as current.

""" + SEASON_SNAPSHOT


# Appended to the per-command prompts so every answer ends with the McLaren angle.
MCLAREN_ANGLE = (
    "\n\nFinish with one short line on what this means for McLaren (Norris and Piastri), "
    "using only the information above. If there is no real McLaren angle, skip that line."
)


def _text_of(content) -> str:
    """Plain-text view of a message body (str, or a multimodal part list)."""
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _trim_messages_to_limit(messages: list, token_limit: int = 30000) -> list:
    """Keep the system prompt and latest message intact; drop oldest history first."""
    def cost(m) -> int:
        return _estimate_tokens(_text_of(m["content"]))

    if sum(cost(m) for m in messages) <= token_limit or len(messages) <= 2:
        return messages

    system_msg, user_msg = messages[0], messages[-1]
    budget = token_limit - cost(system_msg) - cost(user_msg)
    kept: list = []
    # Walk history newest-first so the oldest turns are the ones that fall off.
    for msg in reversed(messages[1:-1]):
        text = _text_of(msg["content"])
        if not isinstance(msg["content"], str) or budget <= 0:
            continue
        allowed = max(budget, 0) * 4
        if len(text) > allowed:
            msg = {**msg, "content": text[:allowed]}
        kept.append(msg)
        budget -= cost(msg)
    return [system_msg] + list(reversed(kept)) + [user_msg]


async def _system_with_live(system: str | None) -> str:
    """Default system prompt plus the auto-refreshed standings block."""
    if system is not None:
        return system
    from utils.mclaren import live_snapshot  # local imports: avoid a cycle
    from utils.news import latest_news
    live, news = await asyncio.gather(live_snapshot(), latest_news())
    return "\n\n".join(p for p in (SYSTEM_PROMPT, live, news) if p)


async def chat(messages: list, model: str = SMART_MODEL, system: str | None = None) -> str:
    full_messages = [{"role": "system", "content": await _system_with_live(system)}] + messages
    full_messages = _trim_messages_to_limit(full_messages)
    max_tokens = 3000  # headroom: reasoning models spend part of this on thinking
    for attempt in range(2):
        data = await _post({
            "model": model,
            "messages": full_messages,
            "temperature": 0.7,
            "max_tokens": max_tokens,
            "reasoning": {"effort": "low"},
        })
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        if choice.get("finish_reason") != "length":
            return text
        logger.warning("%s hit max_tokens=%d (reply len %d)", model, max_tokens, len(text))
        max_tokens *= 2
    return text


async def chat_vision(
    prompt: str,
    image_bytes: bytes,
    mime: str = "image/jpeg",
    history: list | None = None,
    model: str = SMART_MODEL,
) -> str:
    """Answer a question about an image (photo/screenshot) with the main model."""
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"
    user_msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }
    return await chat(messages=list(history or []) + [user_msg], model=model)


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
