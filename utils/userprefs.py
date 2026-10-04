"""Long-term facts about each user (favourite driver, F1 Fantasy team, how they
like answers), saved by the model through the remember_about_user tool and
shown back to it on every question. Unlike utils.convo this never expires;
/me shows what's saved and /me clear wipes it.
"""

from utils import store

_KEY = "user_prefs_v1"
MAX_KEYS = 15
MAX_VALUE = 600

_prefs: dict[str, dict[str, str]] | None = None


def _load() -> dict:
    global _prefs
    if _prefs is None:
        data = store.load(_KEY, {})
        _prefs = data if isinstance(data, dict) else {}
    return _prefs


def get(user_id: int) -> dict[str, str]:
    return dict(_load().get(str(user_id), {}))


def set_fact(user_id: int, key: str, value: str) -> str:
    """Save ``value`` under ``key``; an empty value deletes it. Returns a status line."""
    key = "_".join(key.lower().split())[:40]
    if not key:
        return "Need a key."
    facts = _load().setdefault(str(user_id), {})
    value = " ".join((value or "").split())[:MAX_VALUE]
    if not value:
        facts.pop(key, None)
        status = f"Forgot {key}."
    elif key not in facts and len(facts) >= MAX_KEYS:
        return f"Memory full ({MAX_KEYS} facts). Replace an existing key instead."
    else:
        facts[key] = value
        status = f"Saved {key}."
    store.save(_KEY, _load())
    return status


def clear(user_id: int) -> bool:
    if _load().pop(str(user_id), None) is None:
        return False
    store.save(_KEY, _load())
    return True


def describe(user_id: int) -> str:
    """One line per saved fact, or "" if nothing is saved."""
    return "\n".join(f"- {k}: {v}" for k, v in get(user_id).items())
