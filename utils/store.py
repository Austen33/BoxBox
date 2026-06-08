"""Tiny persistent key/value store backed by a single JSON file.

Used to keep small bits of bot state (``/notify`` subscribers, seen-news
hashes) across restarts, since a polling worker is redeployed often and
in-memory state would otherwise be silently lost.

Writes are atomic (temp file + ``os.replace``) so a crash mid-write can't
corrupt the store. Values must be JSON-serializable.
"""

import json
import logging
import os
import tempfile
import threading

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
_STORE_PATH = os.path.join(DATA_DIR, "state.json")
_lock = threading.Lock()


def _read_all() -> dict:
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("store: could not read %s (%s); starting empty.", _STORE_PATH, e)
        return {}


def _write_all(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _STORE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(key: str, default=None):
    """Return the stored value for ``key`` or ``default`` if absent."""
    with _lock:
        return _read_all().get(key, default)


def save(key: str, value) -> None:
    """Persist ``value`` under ``key`` (write-through, atomic)."""
    with _lock:
        data = _read_all()
        data[key] = value
        try:
            _write_all(data)
        except Exception as e:
            logger.error("store: failed to save key %r: %s", key, e)
