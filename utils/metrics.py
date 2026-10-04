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

Each tracked invocation also opens a *trace* (another context variable): LLM
calls, tool calls and context-building steps are appended to it via
``record_llm`` and ``step``, and when the handler finishes the trace is kept in
a persisted list of recent prompts. ``/cost`` shows what each prompt cost and
``/pipeline`` shows the retrieval pipeline a prompt went through.
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

current_trace: contextvars.ContextVar[dict | None] = contextvars.ContextVar("current_trace", default=None)
_PROMPTS_KEY = "llm_prompts_v1"
_PROMPTS_MAX = 100
_prompts: list | None = None


def _load_daily() -> dict:
    global _daily
    if _daily is None:
        from utils import store
        data = store.load(_DAILY_KEY, {})
        _daily = data if isinstance(data, dict) else {}
    return _daily


def _load_prompts() -> list:
    global _prompts
    if _prompts is None:
        from utils import store
        data = store.load(_PROMPTS_KEY, [])
        _prompts = data if isinstance(data, list) else []
    return _prompts


def step(kind: str, detail: str = "", ms: float | None = None) -> None:
    """Note one pipeline step (context loaded, tool run, ...) on the current prompt's trace."""
    trace = current_trace.get()
    if trace is None:
        return
    started = (time.monotonic() - trace["_start"]) * 1000 - (ms or 0)  # steps are noted when they finish
    trace["steps"].append({"t": max(0, round(started)), "kind": kind,
                           "detail": detail[:160], "ms": round(ms) if ms is not None else None})


def record_llm(usage: dict | None, model: str | None = None, ms: float | None = None) -> None:
    """Add one OpenRouter call's usage to the current command's totals and trace."""
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
    trace = current_trace.get()
    if trace is not None:
        for k, v in row.items():
            trace[k] += v
        step("llm", f"{model or '?'}: {_fmt_tokens(row['prompt'])} in ({_fmt_tokens(row['cached'])} cached), "
                    f"{_fmt_tokens(row['output'])} out, ${row['cost']:.4f}", ms)
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


def _prompt_label(update) -> str:
    """What the user sent, for the /cost and /pipeline listings."""
    query = getattr(update, "callback_query", None)
    if query is not None:
        return f"[button {query.data}]"
    msg = getattr(update, "effective_message", None)
    if msg is None:
        return ""
    if msg.text or msg.caption:
        return (msg.text or msg.caption)[:200]
    for kind in ("voice", "photo", "video", "video_note", "animation", "document"):
        if getattr(msg, kind, None):
            return f"[{kind.replace('_', ' ')}]"
    return ""


def _finish_trace(trace: dict, outcome: str) -> None:
    if not trace["calls"]:
        return  # no LLM involved, nothing to cost
    trace.pop("_start")
    trace["ms"] = round(trace["ms"])
    trace["outcome"] = outcome
    trace["cost"] = round(trace["cost"], 6)
    prompts = _load_prompts()
    prompts.append(trace)
    del prompts[:-_PROMPTS_MAX]
    from utils import store
    store.save(_PROMPTS_KEY, prompts)


def recent_prompts(chat_id: int | None = None, n: int = 10) -> list[dict]:
    """Latest prompts first; all chats when ``chat_id`` is None."""
    rows = [p for p in _load_prompts() if chat_id is None or p.get("chat") == chat_id]
    return rows[::-1][:n]


def track(name: str | None = None):
    """Decorator for ``async def handler(update, context)`` functions."""

    def decorator(func):
        cmd = name or func.__name__.replace("_handler", "")

        @functools.wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            start = time.monotonic()
            token = current_command.set(cmd)
            chat = getattr(update, "effective_chat", None)
            trace = {"ts": time.time(), "cmd": cmd, "chat": chat.id if chat else None,
                     "text": _prompt_label(update), "steps": [], "_start": start,
                     **dict.fromkeys(_LLM_FIELDS, 0)}
            trace_token = current_trace.set(trace)
            try:
                result = await func(update, context, *args, **kwargs)
            except Exception:
                elapsed = (time.monotonic() - start) * 1000
                trace["ms"] = elapsed
                _finish_trace(trace, "error")
                c = _counts[cmd]
                c["error"] += 1
                c["total_ms"] += elapsed
                logger.error("cmd=%s outcome=error latency_ms=%.0f", cmd, elapsed)
                raise
            finally:
                current_command.reset(token)
                current_trace.reset(trace_token)
            elapsed = (time.monotonic() - start) * 1000
            trace["ms"] = elapsed
            _finish_trace(trace, "ok")
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


def _ago(ts: float) -> str:
    s = max(0, time.time() - ts)
    if s < 3600:
        return f"{s / 60:.0f}m ago" if s >= 60 else f"{s:.0f}s ago"
    return f"{s / 3600:.0f}h ago" if s < 86400 else f"{s / 86400:.0f}d ago"


def format_cost(rows: list[dict], all_chats: bool = False) -> str:
    """Plain-text listing of what each recent prompt cost."""
    if not rows:
        return "No prompts recorded yet. Ask me something, then try /cost again."
    lines = [f"Cost per prompt ({'all chats' if all_chats else 'this chat'}, newest first)\n"]
    for p in rows:
        who = f", chat {p['chat']}" if all_chats else ""
        lines.append(
            f"${p['cost']:.4f}  /{p['cmd']}{who}, {_ago(p['ts'])}\n"
            f"  \"{p['text'][:80]}\"\n"
            f"  {p['calls']} LLM calls, {_fmt_tokens(p['prompt'])} in ({_fmt_tokens(p['cached'])} cached), "
            f"{_fmt_tokens(p['output'])} out, {p['ms'] / 1000:.1f}s"
        )
    total = sum(p["cost"] for p in rows)
    lines.append(f"\nTotal for these {len(rows)}: ${total:.4f}, avg ${total / len(rows):.4f} per prompt")
    return "\n".join(lines)


PIPELINE_OVERVIEW = """RAG pipeline (chat, /ask, voice)
1. Context: this chat's last 20 exchanges + saved facts about you (/me)
2. Live context: race-weekend snapshot + latest F1 headlines, in the cached system prompt
3. Retrieval: the model picks tools, run in parallel, up to 4 rounds:
   data (standings, results, qualifying, this weekend, McLaren analysis),
   web_search (Tavily over F1 news sites), FIA documents (list + read)
4. Generation: answer streamed back, grounded in the tool results
5. Memory: exchange saved to the chat; lasting facts via remember_about_user"""

_STEP_ICONS = {"context": "CTX", "live": "LIVE", "llm": "LLM", "tool": "TOOL", "memory": "MEM"}


def format_pipeline(trace: dict | None) -> str:
    """The overview, then the steps the given prompt actually went through."""
    if trace is None:
        return PIPELINE_OVERVIEW + "\n\nNo prompts traced in this chat yet. Ask me something, then /pipeline."
    lines = [PIPELINE_OVERVIEW, "",
             f"Last prompt: /{trace['cmd']}, {_ago(trace['ts'])}", f"\"{trace['text'][:120]}\"", ""]
    for s in sorted(trace["steps"], key=lambda s: s["t"]):
        ms = f" ({s['ms'] / 1000:.1f}s)" if s.get("ms") is not None else ""
        lines.append(f"+{s['t'] / 1000:.1f}s {_STEP_ICONS.get(s['kind'], s['kind'].upper())} {s['detail']}{ms}")
    lines.append(f"\nTotal: {trace['ms'] / 1000:.1f}s, {trace['calls']} LLM calls, ${trace['cost']:.4f}"
                 + ("" if trace.get("outcome") == "ok" else " (failed)"))
    return "\n".join(lines)
