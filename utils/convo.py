"""Per-chat conversation memory so follow-ups ("and Piastri?") have context.

Kept deliberately small: the last few exchanges per chat, expiring after a few
hours of silence, persisted through utils.store so a redeploy doesn't wipe it.
"""

import time

from utils import store

_KEY = "convo_memory_v1"
MAX_MESSAGES = 12          # 6 user/assistant exchanges
TTL_SECONDS = 6 * 3600
_MAX_CHARS = 2000          # per stored message

_mem: dict[str, dict] | None = None


def _load() -> dict:
    global _mem
    if _mem is None:
        data = store.load(_KEY, {})
        _mem = data if isinstance(data, dict) else {}
    return _mem


def _persist() -> None:
    store.save(_KEY, _load())


def get_history(chat_id: int) -> list[dict]:
    """Return recent messages as [{"role", "content"}], oldest first."""
    entry = _load().get(str(chat_id))
    if not entry:
        return []
    if time.time() - entry.get("ts", 0) > TTL_SECONDS:
        _load().pop(str(chat_id), None)
        return []
    return list(entry.get("messages", []))


def add_exchange(chat_id: int, user_text: str, reply: str) -> None:
    mem = _load()
    history = get_history(chat_id)
    history.append({"role": "user", "content": user_text[:_MAX_CHARS]})
    history.append({"role": "assistant", "content": reply[:_MAX_CHARS]})
    mem[str(chat_id)] = {"ts": time.time(), "messages": history[-MAX_MESSAGES:]}
    _persist()


def clear(chat_id: int) -> None:
    if _load().pop(str(chat_id), None) is not None:
        _persist()
