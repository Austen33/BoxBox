"""Helpers for sending Telegram messages safely.

Telegram's legacy Markdown parser raises ``BadRequest`` if the model output
contains stray ``_``, ``*``, ``[`` etc. These helpers retry with no parse mode
so the user always gets *something* back.
"""

import logging
from typing import Any

from telegram import Bot, Message
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

_TELEGRAM_LIMIT = 4096


def _chunk(text: str, limit: int = _TELEGRAM_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > limit:
        # Try to split on a newline before the limit, otherwise hard-cut.
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def safe_reply(
    message: Message,
    text: str,
    parse_mode: str | None = "Markdown",
    **kwargs: Any,
) -> None:
    """Reply to a Telegram message, falling back to plain text on parse errors.

    Also splits messages above Telegram's 4096-character limit.
    """
    parts = _chunk(text)
    for part in parts:
        try:
            await message.reply_text(part, parse_mode=parse_mode, **kwargs)
        except BadRequest as e:
            logger.warning(
                "Telegram rejected message with parse_mode=%s (%s); retrying as plain text.",
                parse_mode,
                e,
            )
            try:
                await message.reply_text(part, parse_mode=None, **kwargs)
            except Exception as e2:
                logger.error("Failed to send fallback plain message: %s", e2)
        except Exception as e:
            logger.error("Failed to send Telegram message: %s", e)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    parse_mode: str | None = "Markdown",
    **kwargs: Any,
) -> None:
    """Send a message to ``chat_id`` via ``bot``, with the same parse-mode
    fallback and chunking as :func:`safe_reply`.

    Use this for scheduler/news pushes that don't have an incoming ``Message``
    to reply to.
    """
    parts = _chunk(text)
    for part in parts:
        try:
            await bot.send_message(chat_id=chat_id, text=part, parse_mode=parse_mode, **kwargs)
        except BadRequest as e:
            logger.warning(
                "Telegram rejected message to %s with parse_mode=%s (%s); retrying as plain text.",
                chat_id,
                parse_mode,
                e,
            )
            try:
                await bot.send_message(chat_id=chat_id, text=part, parse_mode=None, **kwargs)
            except Exception as e2:
                logger.error("Failed to send fallback plain message to %s: %s", chat_id, e2)
        except Exception as e:
            logger.error("Failed to send Telegram message to %s: %s", chat_id, e)
