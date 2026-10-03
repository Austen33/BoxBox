"""In-memory error log so failures can be read from Telegram with /errors.

A logging handler keeps the most recent errors (with tracebacks) in a ring
buffer. Everything is passed through ``redact`` first so API keys, the bot
token and bearer tokens never end up in a chat message.
"""

import logging
import os
import platform
import re
import sys
import traceback
from collections import deque
from datetime import datetime, timezone

BOT_VERSION = "mclaren-v1"
_started = datetime.now(timezone.utc)
_MAX_ENTRIES = 25

_buffer: "deque[str]" = deque(maxlen=_MAX_ENTRIES)

_PATTERNS = [
    re.compile(r"bot\d+:[A-Za-z0-9_-]{10,}"),          # Telegram bot token in URLs
    re.compile(r"\d{8,}:[A-Za-z0-9_-]{30,}"),          # bare Telegram token
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),              # OpenRouter / OpenAI style keys
    re.compile(r"tvly-[A-Za-z0-9_-]{8,}"),             # Tavily keys
    re.compile(r"gsk_[A-Za-z0-9_-]{8,}"),              # Groq keys
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
]
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)


def redact(text: str) -> str:
    # Exact values of secret-looking env vars first, then generic patterns.
    for name, value in os.environ.items():
        if value and len(value) >= 8 and _SECRET_NAME.search(name):
            text = text.replace(value, f"<{name}>")
    for pat in _PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "<redacted>", text)
    return text


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # All errors, plus warnings from our own code (FastF1 is far too chatty).
        own = record.name.startswith(("utils", "handlers", "__main__", "main", "metrics"))
        if record.levelno < logging.ERROR and not (own and record.levelno >= logging.WARNING):
            return
        try:
            stamp = datetime.fromtimestamp(record.created, timezone.utc).strftime("%H:%M:%S")
            lines = [f"[{stamp}] {record.levelname} {record.name}: {record.getMessage()}"]
            if record.exc_info and record.exc_info[0] is not None:
                tb = traceback.format_exception(*record.exc_info)
                lines.append("".join(tb).strip())
            _buffer.append(redact("\n".join(lines)))
        except Exception:
            pass  # never let logging break the bot


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    logging.getLogger().addHandler(_RingHandler(level=logging.WARNING))
    _installed = True


def clear() -> None:
    _buffer.clear()


def diagnostics() -> str:
    def has(name: str) -> str:
        return "set" if os.environ.get(name) else "MISSING"

    uptime = datetime.now(timezone.utc) - _started
    mins = int(uptime.total_seconds() // 60)
    return (
        f"BoxBox {BOT_VERSION} | python {platform.python_version()} | up {mins // 60}h{mins % 60:02d}m\n"
        f"OPEN_ROUTER_KEY: {has('OPEN_ROUTER_KEY')} | TAVILY_API_KEY: {has('TAVILY_API_KEY')} | "
        f"TELEGRAM_CHAT_ID/ADMIN_CHAT_ID: {'set' if (os.environ.get('ADMIN_CHAT_ID') or os.environ.get('TELEGRAM_CHAT_ID')) else 'MISSING'}\n"
        f"ffmpeg: {'found' if __import__('shutil').which('ffmpeg') else 'not found'}\n"
        f"OPEN_ROUTER_KEY check: {_key_check()}"
    )


def _key_check() -> str:
    try:
        from utils.groq_client import key_fingerprint
        return key_fingerprint(os.environ.get("OPEN_ROUTER_KEY"))
    except Exception as e:
        return f"unavailable ({type(e).__name__})"


def report(n: int = 5) -> str:
    n = max(1, min(n, _MAX_ENTRIES))
    recent = list(_buffer)[-n:]
    body = "\n\n".join(recent) if recent else "No errors recorded since the bot started."
    return f"{diagnostics()}\n\n--- last {len(recent)} of {len(_buffer)} logged errors (newest last) ---\n{body}"
