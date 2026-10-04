import asyncio
import base64
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from utils.metrics import record_llm

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


def _with_fallbacks(payload: dict) -> dict:
    """Add OpenRouter's `models` failover list: if the primary model errors or has
    been retired, the request moves to the next one instead of failing."""
    payload = {**payload, "usage": {"include": True}}  # token counts and cost, for /stats
    model = payload.get("model")
    if not model or "models" in payload:
        return payload
    chain: list[str] = []
    for m in [model, *FALLBACK_MODELS, SMART_MODEL, FAST_MODEL]:
        if m and m not in chain:
            chain.append(m)
    return {**payload, "models": chain[:3]}


async def _post(payload: dict, attempts: int = 3) -> dict:
    """POST a chat completion to OpenRouter, retrying on 429/5xx."""
    payload = _with_fallbacks(payload)
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
        record_llm(data.get("usage"))
        return data
    raise RuntimeError("OpenRouter request failed")


# Cheap and fast for short lookups; stronger model for reasoning-heavy answers.
FAST_MODEL = os.getenv("FAST_MODEL", "openai/gpt-6-luna")
SMART_MODEL = os.getenv("SMART_MODEL", "anthropic/claude-sonnet-5.5")
# Photos/screenshots. Defaults to the smart model, which accepts images.
VISION_MODEL = os.getenv("VISION_MODEL", SMART_MODEL)
STT_MODEL = os.getenv("STT_MODEL", "google/gemini-3.5-flash-lite")
# Video clips (onboards, replays, GIFs). Needs a model that accepts video input.
VIDEO_MODEL = os.getenv("VIDEO_MODEL", "google/gemini-3.8-flash")
# Extra failover models (comma-separated), tried before the other tier's model.
FALLBACK_MODELS = [m.strip() for m in os.getenv("FALLBACK_MODELS", "").split(",") if m.strip()]
TTS_MODEL = os.getenv("TTS_MODEL", "openai/gpt-audio-mini")
# gpt-audio voices: alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar
TTS_VOICE = os.getenv("TTS_VOICE", "cedar")

# --- edge-tts (primary TTS) -----------------------------------------------
# Microsoft neural voices via Edge read-aloud. Free, no API key required.
# Default: en-IE-ConnorNeural, an Irish male voice (en-IE-EmilyNeural is the
# Irish female one). Override via EDGE_TTS_VOICE. Full list: `edge-tts --list-voices`
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "en-IE-ConnorNeural")

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
        tts = gTTS(text[:4096], lang="en", tld="ie")  # Irish-accented Google voice
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
                    "exactly as written, in a natural Irish accent. "
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

    Engine priority: edge-tts (Irish voice) -> OpenRouter audio model -> gTTS.
    """
    cleaned = _strip_markdown(_strip_emotion_tags(text))
    if len(cleaned) > 4096:
        cleaned = cleaned[:4096]

    source_bytes: bytes | None = None
    source_fmt = "mp3"
    convert_speed = 1.0

    # 1. edge-tts: Irish voice (Connor), free, no API key -> MP3
    try:
        source_bytes = await _edge_tts_mp3(cleaned)
        source_fmt = "mp3"
        convert_speed = TTS_SPEED
        logger.info("TTS: edge-tts %s OK (%d bytes)", EDGE_TTS_VOICE, len(source_bytes))
    except Exception:
        logger.warning("edge-tts failed, trying OpenRouter TTS", exc_info=True)

    # 2. OpenRouter audio model (raw PCM16 -> needs ffmpeg to be playable)
    if not source_bytes and _find_ffmpeg():
        try:
            pcm = await _openrouter_tts_pcm(cleaned)
            if pcm:
                source_bytes, source_fmt, convert_speed = pcm, "s16le", 1.0
                logger.info("TTS: %s OK (%d bytes)", TTS_MODEL, len(pcm))
        except Exception:
            logger.warning("OpenRouter TTS failed, using gTTS fallback", exc_info=True)

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

SEASON_SNAPSHOT = """SEASON BACKGROUND (written 3 October 2026, after round 15 of the 2026 season). This is narrative background only. The LIVE DATA block and any live F1 data or search results in the conversation are newer and override it. 

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
- McLaren is your team, so when a question is about standings, results or a race, include where Norris and Piastri are in a few words. Don't add McLaren to answers it isn't relevant to.
- Be a fan, not a cheerleader. Celebrate wins and good drives, but be straight about bad weekends, mistakes, strategy calls that went wrong and pace deficits. Never spin a result. Never put down rival drivers or teams, give them credit where it is earned.
- Treat Norris and Piastri evenly. Do not pick a favourite or stir up a rivalry. Report the numbers and let them speak.
- McLaren history is fair game: Senna, Prost, Hakkinen, Hamilton, Button, Norris, the 1988 season, the 2025 title, and so on. Same rule as everything else, only state history you are sure of.

How to answer (most important):
- Answer exactly what was asked, in the first sentence. No warm-up, no restating the question.
- Be short. A simple factual question gets one or two sentences. Anything else stays under about 100 words unless the user asks for detail or an explanation.
- Only add context that changes or explains the answer. No background they didn't ask for, no recap of the season, no summary at the end, no "let me know if...", no offers of more help.
- Only mention McLaren when the question is about McLaren or McLaren is directly affected, and then in one short sentence at most.
- No caveats or source talk unless the answer is genuinely uncertain, then one short clause. Never mention "my data", "the data you've got", "the live data block" or how you found something.
- Don't revisit or correct earlier messages unless one was clearly wrong, and then fix it in one short sentence.

Style:
- UK English spelling and terms: tyre, colour, favourite, centre, defence, programme, analyse, realise, metres, kilometres.
- Never use em dashes or en dashes. Use a comma, a full stop or brackets instead.
- Plain, natural, conversational. No textbook or news-article tone.
- No filler phrases: "it is worth noting", "it's worth mentioning", "dive into", "delve", "certainly", "needless to say", "genuinely", "at the end of the day", "it's important to note", "fascinatingly", "in summary".
- Prose by default. Use a short list only for results or genuinely list-shaped answers.
- Format for Telegram: *bold* sparingly for the key fact, short paragraphs.

Accuracy:
- Never invent results, lap times, quotes, penalties, upgrades or team news. If you don't have something, say so in a few words.
- Treat driver and team information from memory as possibly outdated; rely on the live data and news provided.
- The current year is 2026. If a source talks about 2025, that's last season.

""" + SEASON_SNAPSHOT


# Appended to the per-command prompts so every answer ends with the McLaren angle.
MCLAREN_ANGLE = (
    "\n\nKeep it tight: no intro, no filler, no closing summary. UK English, no em or en dashes. "
    "If McLaren (Norris, Piastri) is directly affected, end with one short sentence on it; otherwise don't mention them."
)


def _text_of(content) -> str:
    """Plain-text view of a message body (str, or a multimodal part list)."""
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _trim_messages_to_limit(messages: list, token_limit: int = 100000) -> list:
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


def _supports_cache_control(model: str) -> bool:
    # OpenAI models cache automatically; Anthropic and Gemini need explicit breakpoints.
    return model.startswith(("anthropic/", "google/"))


async def _system_message(system: str | None, extra_system: str | None, model: str) -> dict:
    """System message: the fixed prompt first, then the auto-refreshed live data
    and news. Each part gets a prompt-cache breakpoint, so the long fixed prompt
    (plus the tool definitions ahead of it) is only billed in full once. The fixed
    part is cached for an hour so it survives quiet spells between messages; the
    live part changes often, so it keeps the default five minutes."""
    if system is not None:
        static, dynamic = system, ""
    else:
        from utils.mclaren import live_snapshot  # local imports: avoid a cycle
        from utils.news import latest_news
        live, news = await asyncio.gather(live_snapshot(), latest_news())
        static = "\n\n".join(p for p in (SYSTEM_PROMPT, extra_system) if p)
        dynamic = "\n\n".join(p for p in (live, news) if p)
    if not _supports_cache_control(model):
        return {"role": "system", "content": "\n\n".join(p for p in (static, dynamic) if p)}
    parts = [{"type": "text", "text": static, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    if dynamic:
        parts.append({"type": "text", "text": dynamic, "cache_control": {"type": "ephemeral"}})
    return {"role": "system", "content": parts}


def _cache_history(convo: list, model: str) -> list:
    """Put a cache breakpoint on the latest user message, so the conversation so
    far is read from cache on the next tool round and the next turn. (The system
    message holds two breakpoints; the limit is four.)"""
    if not _supports_cache_control(model):
        return convo
    for i in range(len(convo) - 1, 0, -1):
        msg = convo[i]
        if msg.get("role") != "user":
            continue
        content = msg["content"]
        if isinstance(content, str):
            parts = [{"type": "text", "text": content}]
        else:
            parts = [dict(p) for p in content]
        if not parts:
            return convo
        parts[-1]["cache_control"] = {"type": "ephemeral"}
        return convo[:i] + [{**msg, "content": parts}] + convo[i + 1:]
    return convo


_US_TO_UK = {
    "tire": "tyre", "tires": "tyres", "color": "colour", "colors": "colours", "colored": "coloured",
    "favorite": "favourite", "favorites": "favourites", "center": "centre", "centers": "centres",
    "defense": "defence", "offense": "offence", "analyze": "analyse", "analyzed": "analysed",
    "analyzing": "analysing", "realize": "realise", "realized": "realised", "organize": "organise",
    "organized": "organised", "recognize": "recognise", "recognized": "recognised",
    "apologize": "apologise", "apologized": "apologised", "behavior": "behaviour",
    "maneuver": "manoeuvre", "maneuvers": "manoeuvres", "gray": "grey", "meters": "metres",
    "kilometers": "kilometres", "liters": "litres", "practicing": "practising",
    "criticize": "criticise", "criticized": "criticised", "minimize": "minimise",
    "maximize": "maximise", "optimize": "optimise", "optimized": "optimised",
    "prioritize": "prioritise", "prioritized": "prioritised", "penalize": "penalise",
    "penalized": "penalised", "jewelry": "jewellery", "program": "programme",
}
_US_RE = re.compile(r"\b(" + "|".join(_US_TO_UK) + r")\b", re.IGNORECASE)


def _uk_word(m: re.Match) -> str:
    word = m.group(0)
    uk = _US_TO_UK[word.lower()]
    if word.isupper():
        return uk.upper()
    return uk[0].upper() + uk[1:] if word[0].isupper() else uk


def tidy(text: str) -> str:
    """Final pass on every reply: no em/en dashes, UK spelling."""
    if not text:
        return text
    # — is an em dash, – an en dash (escaped so the literal characters
    # never appear in this file).
    text = re.sub(r"(\d)\s*[–—]\s*(\d)", r"\1-\2", text)  # ranges: 1-2
    text = re.sub(r"\s*[–—]\s*", ", ", text)              # punctuation dash -> comma
    text = text.replace(", ,", ",").replace(",.", ".")
    return _US_RE.sub(_uk_word, text)


@dataclass
class Tool:
    """A function the model may call. ``fn`` takes the JSON arguments as keyword
    arguments and returns text for the model to read."""
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Awaitable[str]]

    def spec(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}


OnText = Callable[[str], Awaitable[None]]

_TOOL_RESULT_LIMIT = 20000  # characters per tool result


async def _run_tool(call: dict, tools: dict[str, Tool]) -> str:
    fn = call.get("function") or {}
    tool = tools.get(fn.get("name", ""))
    if tool is None:
        return f"Unknown tool {fn.get('name')!r}."
    try:
        args = json.loads(fn.get("arguments") or "{}") or {}
    except json.JSONDecodeError:
        return "Tool arguments were not valid JSON."
    try:
        result = await tool.fn(**args)
    except TypeError as e:
        return f"Bad arguments for {tool.name}: {e}"
    except Exception as e:
        logger.warning("tool %s failed", tool.name, exc_info=True)
        return f"{tool.name} failed: {type(e).__name__}. Answer without it."
    logger.info("tool %s(%s) -> %d chars", tool.name, fn.get("arguments"), len(result or ""))
    return (result or "No data.")[:_TOOL_RESULT_LIMIT]


def _merge_reasoning(acc: list[dict], deltas: list[dict]) -> None:
    """Stitch streamed reasoning_details fragments back into whole blocks, so they
    can be passed back to the model on the next tool round."""
    for d in deltas:
        idx = d.get("index")
        target = None
        if idx is not None:
            target = next((x for x in acc if x.get("index") == idx), None)
        elif acc and acc[-1].get("type") == d.get("type"):
            target = acc[-1]
        if target is None:
            acc.append(dict(d))
            continue
        for k, v in d.items():
            if k in ("text", "summary", "data") and isinstance(v, str):
                target[k] = (target.get(k) or "") + v
            elif v is not None:
                target[k] = v


async def _post_stream(payload: dict, on_text: OnText, attempts: int = 3) -> dict:
    """Streamed completion. Calls ``on_text`` with the reply so far as it arrives
    and returns the assembled response in the same shape as ``_post``."""
    payload = _with_fallbacks({**payload, "stream": True})
    delay = 1.0
    for attempt in range(attempts):
        content, finish, model, usage = "", None, payload.get("model"), None
        calls: dict[int, dict] = {}
        reasoning: list[dict] = []
        async with _get_client().stream(
            "POST", OPENROUTER_URL, headers=_headers(), json=payload
        ) as resp:
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                logger.warning("OpenRouter %s, retrying in %.0fs", resp.status_code, delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 400:
                body = (await resp.aread()).decode(errors="replace")
                raise RuntimeError(
                    f"OpenRouter HTTP {resp.status_code} for model {payload.get('model')}: {body[:300]}"
                )
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
                if "error" in chunk:
                    raise RuntimeError(f"OpenRouter error: {chunk['error']}")
                model = chunk.get("model", model)
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content += delta["content"]
                        await on_text(tidy(content))
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {
                            "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                        })
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        slot["function"]["name"] += fn.get("name") or ""
                        slot["function"]["arguments"] += fn.get("arguments") or ""
                    if delta.get("reasoning_details"):
                        _merge_reasoning(reasoning, delta["reasoning_details"])
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        message: dict = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        if reasoning:
            message["reasoning_details"] = reasoning
        record_llm(usage)
        return {"model": model, "usage": usage, "choices": [{"message": message, "finish_reason": finish}]}
    raise RuntimeError("OpenRouter request failed")


async def chat(
    messages: list,
    model: str = SMART_MODEL,
    system: str | None = None,
    *,
    effort: str = "low",
    tools: list[Tool] | None = None,
    extra_system: str | None = None,
    on_text: OnText | None = None,
    max_tool_rounds: int = 4,
) -> str:
    """Chat completion with the BoxBox system prompt.

    effort: reasoning effort ("minimal", "low", "medium", "high"); spend more on
      analysis (/predict, /strategy) and less on rewrites and classification.
    tools: functions the model may call to fetch data before answering; rounds
      run until it answers, and the last round forces a text answer.
    extra_system: static instructions appended to the cached system prompt.
    on_text: async callback given the reply so far while it streams.
    """
    convo = [await _system_message(system, extra_system, model)] + list(messages)
    convo = _cache_history(_trim_messages_to_limit(convo), model)
    registry = {t.name: t for t in tools or []}
    max_tokens = 3000  # headroom: reasoning models spend part of this on thinking
    rounds = 0
    retried_length = False
    while True:
        payload = {
            "model": model,
            "messages": convo,
            "max_tokens": max_tokens,
            "reasoning": {"effort": effort},
        }
        if registry:
            payload["tools"] = [t.spec() for t in registry.values()]
            if rounds >= max_tool_rounds:
                payload["tool_choice"] = "none"
        data = await (_post_stream(payload, on_text) if on_text else _post(payload))
        choice = data["choices"][0]
        message = choice["message"]
        calls = message.get("tool_calls") or []
        if calls and registry and rounds < max_tool_rounds:
            rounds += 1
            turn = {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
            if message.get("reasoning_details"):
                turn["reasoning_details"] = message["reasoning_details"]
            results = await asyncio.gather(*(_run_tool(c, registry) for c in calls))
            convo += [turn] + [
                {"role": "tool", "tool_call_id": c.get("id", ""), "content": r}
                for c, r in zip(calls, results)
            ]
            continue
        text = message.get("content") or ""
        if choice.get("finish_reason") == "length" and not retried_length:
            logger.warning("%s hit max_tokens=%d (reply len %d)", model, max_tokens, len(text))
            max_tokens *= 2
            retried_length = True
            continue
        return tidy(text)


def _json_from(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text or "", re.DOTALL)
        if not m:
            raise ValueError(f"model returned no JSON: {text[:200]!r}")
        return json.loads(m.group(0))


async def chat_json(
    messages: list,
    schema: dict,
    name: str,
    model: str = FAST_MODEL,
    system: str = "You extract structured data. Output JSON only.",
    effort: str = "minimal",
) -> dict:
    """Structured output: the reply is constrained to ``schema`` (strict JSON
    schema, so every property must be listed in ``required``)."""
    data = await _post({
        "model": model,
        "messages": [{"role": "system", "content": system}] + list(messages),
        "max_tokens": 3000,
        "reasoning": {"effort": effort},
        # Only route to providers that honour every parameter, so the schema is
        # never silently ignored.
        "provider": {"require_parameters": True},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        },
    })
    return _json_from(data["choices"][0]["message"].get("content") or "")


def image_part(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"
    return {"type": "image_url", "image_url": {"url": data_url}}


def video_part(video_bytes: bytes, mime: str = "video/mp4") -> dict:
    """A video clip, for a model that accepts video input (VIDEO_MODEL)."""
    data_url = f"data:{mime};base64,{base64.b64encode(video_bytes).decode()}"
    return {"type": "video_url", "video_url": {"url": data_url}}


def pdf_part(pdf_bytes: bytes, filename: str = "document.pdf") -> dict:
    """A PDF the model reads directly (text, tables and layout)."""
    data_url = f"data:application/pdf;base64,{base64.b64encode(pdf_bytes).decode()}"
    return {"type": "file", "file": {"filename": filename, "file_data": data_url}}


async def chat_vision(
    prompt: str,
    image_bytes: bytes,
    mime: str = "image/jpeg",
    history: list | None = None,
    model: str = VISION_MODEL,
    **kwargs,
) -> str:
    """Answer a question about an image (photo/screenshot) with the vision model.
    Extra keyword arguments (tools, effort, on_text...) go to ``chat``."""
    return await chat_attachment(prompt, image_part(image_bytes, mime), history, model, **kwargs)


async def chat_attachment(
    prompt: str,
    attachment: dict,
    history: list | None = None,
    model: str = VISION_MODEL,
    **kwargs,
) -> str:
    """Answer a question about an attached image or PDF content part."""
    user_msg = {"role": "user", "content": [{"type": "text", "text": prompt}, attachment]}
    return await chat(messages=list(history or []) + [user_msg], model=model, **kwargs)


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
