"""Lightweight in-process observability for command handlers.

``track(name)`` wraps a Telegram handler so every invocation emits one
structured log line (command, latency, outcome) and bumps per-command
counters. ``format_stats()`` renders those counters for an admin ``/stats``
command. Near-zero overhead; counters reset on restart (this is a single
polling worker, not a metrics backend).

LLM usage (tokens and OpenRouter's reported cost) is recorded per command via
``record_llm``; the command comes from a context variable set by ``track``, so
calls made inside tools and gathered tasks are still attributed. Daily cost
totals are persisted so they survive redeploys.
"""

import contextvars
import datetime
import functools
import logging
import time
from collections import defaultdict

logger = logging.getLogger("metrics")

_counts: "defaultdict[str, dict]" = defaultdict(
    lambda: {"ok": 0, "error": 0, "total_ms": 0.0}
)

current_command: contextvars.ContextVar[str] = contextvars.ContextVar("current_command", default="background")

_LLM_FIELDS = ("calls", "prompt", "cached", "output", "cost")
_llm: "defaultdict[str, dict]" = defaultdict(lambda: dict.fromkeys(_LLM_FIELDS, 0))
_DAILY_KEY = "llm_cost_daily_v1"
_DAILY_DAYS = 30
_daily: dict | None = None
_daily_saved = 0.0


def _load_daily() -> dict:
    global _daily
    if _daily is None:
        from utils import store
        data = store.load(_DAILY_KEY, {})
        _daily = data if isinstance(data, dict) else {}
    return _daily


def record_llm(usage: dict | None) -> None:
    """Add one OpenRouter call's usage to the current command's totals."""
    global _daily_saved
    if not usage:
        return
    details = usage.get("prompt_tokens_details") or {}
    row = {
        "calls": 1,
        "prompt": usage.get("prompt_tokens") or 0,
        "cached": details.get("cached_tokens") or 0,
        "output": usage.get("completion_tokens") or 0,
        "cost": float(usage.get("cost") or 0),
    }
    totals = _llm[current_command.get()]
    for k, v in row.items():
        totals[k] += v
    daily = _load_daily()
    day = daily.setdefault(datetime.date.today().isoformat(), dict.fromkeys(_LLM_FIELDS, 0))
    for k, v in row.items():
        day[k] = day.get(k, 0) + v
    for old in sorted(daily)[:-_DAILY_DAYS]:
        daily.pop(old)
    if time.monotonic() - _daily_saved > 60:  # throttle writes; a restart loses at most a minute
        from utils import store
        store.save(_DAILY_KEY, daily)
        _daily_saved = time.monotonic()


def _fmt_tokens(n: float) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(int(n))


def track(name: str | None = None):
    """Decorator for ``async def handler(update, context)`` functions."""

    def decorator(func):
        cmd = name or func.__name__.replace("_handler", "")

        @functools.wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            start = time.monotonic()
            token = current_command.set(cmd)
            try:
                result = await func(update, context, *args, **kwargs)
            except Exception:
                elapsed = (time.monotonic() - start) * 1000
                c = _counts[cmd]
                c["error"] += 1
                c["total_ms"] += elapsed
                logger.error("cmd=%s outcome=error latency_ms=%.0f", cmd, elapsed)
                raise
            finally:
                current_command.reset(token)
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
    lines = ["*Command stats* (since boot)\n"]
    if not _counts:
        lines.append("No commands recorded yet.")
    for cmd in sorted(_counts):
        c = _counts[cmd]
        n = c["ok"] + c["error"]
        avg = c["total_ms"] / n if n else 0
        lines.append(f"`{cmd}`: {n} calls, {c['error']} err, avg {avg:.0f}ms")

    if _llm:
        lines.append("\n*LLM usage* (since boot; tokens in / cached / out, cost)\n")
        for cmd, t in sorted(_llm.items(), key=lambda kv: -kv[1]["cost"]):
            n = _counts[cmd]["ok"] + _counts[cmd]["error"] if cmd in _counts else 0
            per = f", ${t['cost'] / n:.4f}/use" if n else ""
            lines.append(
                f"`{cmd}`: {t['calls']} calls, {_fmt_tokens(t['prompt'])} / {_fmt_tokens(t['cached'])} / "
                f"{_fmt_tokens(t['output'])}, ${t['cost']:.4f}{per}"
            )
        total = sum(t["cost"] for t in _llm.values())
        lines.append(f"Total: ${total:.4f}")

    daily = _load_daily()
    if daily:
        lines.append("\n*LLM cost by day* (persisted)\n")
        for day in sorted(daily)[-7:]:
            d = daily[day]
            lines.append(f"{day}: ${d['cost']:.4f}, {d['calls']} calls, {_fmt_tokens(d['prompt'])} in "
                         f"({_fmt_tokens(d['cached'])} cached), {_fmt_tokens(d['output'])} out")
        last30 = sum(d["cost"] for d in daily.values())
        lines.append(f"Last {len(daily)} days: ${last30:.4f}")
    return "\n".join(lines)
