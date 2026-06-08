"""Admin-gating helpers.

A small number of commands (debug/diagnostics like ``/testvoice`` and ``/stats``)
should only be callable by the bot owner. The owner's chat id comes from the
``ADMIN_CHAT_ID`` env var, falling back to ``TELEGRAM_CHAT_ID`` if unset.
"""

import os


def get_admin_chat_id() -> int | None:
    raw = os.environ.get("ADMIN_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def is_admin(chat_id: int | None) -> bool:
    admin = get_admin_chat_id()
    return admin is not None and chat_id == admin
