"""Lightweight in-process observability for command handlers.

``track(name)`` wraps a Telegram handler so every invocation emits one
structured log line (command, latency, outcome) and bumps per-command
counters. ``format_stats()`` renders those counters for an admin ``/stats``
command. Near-zero overhead; counters reset on restart (this is a single
polling worker, not a metrics backend).
"""

import functools
import logging
import time
from collections import defaultdict

logger = logging.getLogger("metrics")

_counts: "defaultdict[str, dict]" = defaultdict(
    lambda: {"ok": 0, "error": 0, "total_ms": 0.0}
)


def track(name: str | None = None):
    """Decorator for ``async def handler(update, context)`` functions."""

    def decorator(func):
        cmd = name or func.__name__.replace("_handler", "")

        @functools.wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            start = time.monotonic()
            try:
                result = await func(update, context, *args, **kwargs)
            except Exception:
                elapsed = (time.monotonic() - start) * 1000
                c = _counts[cmd]
                c["error"] += 1
                c["total_ms"] += elapsed
                logger.error("cmd=%s outcome=error latency_ms=%.0f", cmd, elapsed)
                raise
            elapsed = (time.monotonic() - start) * 1000
            c = _counts[cmd]
            c["ok"] += 1
            c["total_ms"] += elapsed
            logger.info("cmd=%s outcome=ok latency_ms=%.0f", cmd, elapsed)
            return result

        return wrapper

    return decorator


def snapshot() -> dict:
    """Return a plain-dict copy of the current counters."""
    return {k: dict(v) for k, v in _counts.items()}


def format_stats() -> str:
    if not _counts:
        return "No commands recorded yet."
    lines = ["*Command stats* (since boot)\n"]
    for cmd in sorted(_counts):
        c = _counts[cmd]
        n = c["ok"] + c["error"]
        avg = c["total_ms"] / n if n else 0
        lines.append(f"`{cmd}`: {n} calls, {c['error']} err, avg {avg:.0f}ms")
    return "\n".join(lines)
