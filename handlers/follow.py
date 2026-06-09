"""Personalized alerts: /follow and /unfollow a driver or team.

Followed entities are persisted per chat via :mod:`utils.store` (so they
survive restarts). Each entry carries lower-case ``keywords`` used to match
against breaking-news text, so the news watcher can flag items that mention
something a user follows (see :func:`match_follows`, consumed by
``handlers.notify``).
"""

import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from utils.f1_data import get_current_season
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply
from utils import store
from handlers.history import _fetch_driver_id_by_name
from handlers.profile import (
    _fetch_driver_bio,
    _fetch_constructor_id_by_name,
    _fetch_constructor_bio,
)

logger = logging.getLogger(__name__)

_FOLLOWS_KEY = "follows"
_MAX_FOLLOWS = 10


# --- Storage (chat_id -> list of follow entries) -------------------------

def _load_all() -> dict:
    data = store.load(_FOLLOWS_KEY, {})
    return data if isinstance(data, dict) else {}


def _get(chat_id: int) -> list[dict]:
    return _load_all().get(str(chat_id), [])


def _set(chat_id: int, entries: list[dict]) -> None:
    data = _load_all()
    if entries:
        data[str(chat_id)] = entries
    else:
        data.pop(str(chat_id), None)
    store.save(_FOLLOWS_KEY, data)


def match_follows(chat_id: int, text: str) -> list[str]:
    """Return the labels this chat follows that are mentioned in ``text``.

    Keywords are matched on word boundaries so a short code like ``ver``
    doesn't spuriously hit ``silVERstone``.
    """
    text_lower = text.lower()
    hits = []
    for e in _get(chat_id):
        for kw in e.get("keywords", []):
            if kw and re.search(rf"\b{re.escape(kw)}\b", text_lower):
                hits.append(e["label"])
                break
    return hits


# --- Resolution ----------------------------------------------------------

async def _driver_entry(driver_id: str, fallback_label: str) -> dict | None:
    bio = await _fetch_driver_bio(driver_id)
    if not bio:
        return None
    family = bio.get("familyName", "")
    code = bio.get("code", "")
    label = f"{bio.get('givenName', '')} {family}".strip() or fallback_label
    keywords = {label.lower(), family.lower(), code.lower()}
    return {
        "type": "driver",
        "id": driver_id,
        "label": label,
        "keywords": sorted(k for k in keywords if k),
    }


async def _resolve_entry(text: str) -> dict | None:
    """Resolve free-form input to a follow entry for a current-grid driver/team.

    Resolution is scoped strictly to the current (2026) season: only this
    year's drivers and constructors can be followed. Anything not on the
    current grid — retired drivers, defunct teams, arbitrary text — returns
    ``None`` so the caller can reject it. Precedence is driver → team.
    """
    season = str(get_current_season())

    driver_id = await _fetch_driver_id_by_name(text, include_historical=False, season=season)
    if driver_id:
        entry = await _driver_entry(driver_id, text)
        if entry:
            return entry

    constructor_id = await _fetch_constructor_id_by_name(text, season=season)
    if constructor_id:
        bio = await _fetch_constructor_bio(constructor_id)
        label = (bio.get("name") if bio else None) or text.title()
        keywords = {label.lower(), constructor_id.replace("_", " ")}
        return {
            "type": "team",
            "id": constructor_id,
            "label": label,
            "keywords": sorted(keywords),
        }

    return None


# --- Handlers ------------------------------------------------------------

async def follow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    args = context.args or []
    entries = _get(chat_id)

    if not args:
        if entries:
            labels = "\n".join(f"• {e['label']}" for e in entries)
            await update.message.reply_text(
                f"You follow:\n{labels}\n\n"
                "Add more with /follow [driver or team]. Remove with /unfollow [name]."
            )
        else:
            await update.message.reply_text(
                "You're not following anyone yet.\n\n"
                "Follow a driver or team and their breaking news gets flagged for you:\n"
                "/follow VER\n/follow Ferrari"
            )
        return

    text = " ".join(args)
    await update.message.reply_chat_action("typing")
    entry = await _resolve_entry(text)

    if entry is None:
        await update.message.reply_text(
            f"Couldn't find a 2026 driver or team called '{text}'.\n"
            "You can only follow drivers and teams on the current grid — "
            "try a 3-letter code (VER, NOR, LEC) or a team name (Ferrari, McLaren)."
        )
        return

    if any(e["type"] == entry["type"] and e["id"] == entry["id"] for e in entries):
        await update.message.reply_text(f"You already follow {entry['label']}.")
        return

    if len(entries) >= _MAX_FOLLOWS:
        await update.message.reply_text(
            f"You can follow up to {_MAX_FOLLOWS}. Unfollow someone first with /unfollow."
        )
        return

    entries.append(entry)
    _set(chat_id, entries)
    await safe_reply(
        update.message,
        f"Now following *{entry['label']}*. Their breaking news will be flagged for you.\n"
        "Use /unfollow to stop, or /follow to see your list.",
    )


async def unfollow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    entries = _get(chat_id)
    if not entries:
        await update.message.reply_text("You're not following anyone.")
        return

    args = context.args or []
    if not args:
        labels = "\n".join(f"• {e['label']}" for e in entries)
        await update.message.reply_text(
            f"You follow:\n{labels}\n\nRemove one with /unfollow [name], e.g. /unfollow VER."
        )
        return

    text = " ".join(args).lower()
    removed = None
    kept = []
    for e in entries:
        is_match = (
            text in e["label"].lower()
            or text == e["id"].lower()
            or any(text == kw or text in kw for kw in e.get("keywords", []))
        )
        if removed is None and is_match:
            removed = e
        else:
            kept.append(e)

    if removed is None:
        await update.message.reply_text(f"You're not following '{' '.join(args)}'.")
        return

    _set(chat_id, kept)
    await update.message.reply_text(f"Unfollowed {removed['label']}.")
