import logging
from telegram import Update
from telegram.ext import ContextTypes
from utils.groq_client import chat, SMART_MODEL
from utils import convo, userprefs
from utils.f1_tools import TOOL_RULES, f1_tools
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply, stream_reply

logger = logging.getLogger(__name__)

VOICE_RULES = (
    "\n\n(This reply will be spoken as a voice note. Two to four short sentences, about 60 words at most. "
    "Answer the question in the first sentence. Talk like a person, not a document: contractions, "
    "short sentences, no lists, no markdown or symbols. Say 'Formula One' not 'F1', 'fifth' not 'P5', "
    "and say numbers the way you'd speak them. No intro and no sign-off.)"
)


def user_context(user_id: int | None) -> str:
    """What we've saved about this user, as a preface for their message."""
    facts = userprefs.describe(user_id) if user_id else ""
    return f"(What you know about this user from earlier chats:\n{facts})\n\n" if facts else ""


async def get_f1_response(
    query: str,
    for_voice: bool = False,
    history: list[dict] | None = None,
    user_id: int | None = None,
    on_text=None,
) -> str:
    """Answer an F1 question. The model fetches whatever data it needs through
    tools (standings, results, McLaren analysis, search, FIA documents), using
    the conversation history to resolve follow-ups like "and Piastri?"."""
    content = user_context(user_id) + query + (VOICE_RULES if for_voice else "")
    return await chat(
        messages=list(history or []) + [{"role": "user", "content": content}],
        model=SMART_MODEL,
        tools=f1_tools(user_id),
        extra_system=TOOL_RULES,
        on_text=on_text,
    )


async def answer_and_remember(
    chat_id: int, query: str, for_voice: bool = False, user_id: int | None = None, on_text=None,
) -> str:
    """Answer with this chat's recent history, then store the exchange."""
    history = convo.get_history(chat_id)
    response = await get_f1_response(
        query, for_voice=for_voice, history=history, user_id=user_id, on_text=on_text,
    )
    convo.add_exchange(chat_id, query, response)
    return response


async def _answer_streamed(update: Update, query: str) -> str:
    message = update.message
    return await stream_reply(message, lambda on_text: answer_and_remember(
        update.effective_chat.id, query, user_id=update.effective_user.id, on_text=on_text,
    ))


async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down, one question at a time.")
        return

    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("What do you want to know? Try /ask who has the most wins at Monaco")
        return

    await update.message.reply_chat_action("typing")
    await _answer_streamed(update, query)
    await _maybe_grid(update.message, query)


async def _maybe_grid(message, query: str) -> None:
    """Attach the grid graphic to grid questions."""
    from handlers.grid_cmd import wants_grid, send_grid_image
    if wants_grid(query):
        await message.reply_chat_action("upload_photo")
        await send_grid_image(message)


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain (non-command) text in a private chat: a normal conversation."""
    if not update.message or not update.message.text:
        return
    if is_rate_limited(update.effective_user.id):
        await update.message.reply_text("Slow down, one question at a time.")
        return
    await update.message.reply_chat_action("typing")
    await _answer_streamed(update, update.message.text)
    await _maybe_grid(update.message, update.message.text)


async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    convo.clear(update.effective_chat.id)
    await update.message.reply_text(
        "Fresh start. I've cleared our conversation (/me shows what I remember about you long-term)."
    )


async def me_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/me shows the long-term facts saved about you; /me clear forgets them."""
    user_id = update.effective_user.id
    if context.args and context.args[0].lower() in ("clear", "forget", "reset"):
        done = userprefs.clear(user_id)
        await update.message.reply_text("Done, I've forgotten everything about you." if done
                                        else "I wasn't remembering anything about you.")
        return
    facts = userprefs.describe(user_id)
    if not facts:
        await update.message.reply_text(
            "I don't remember anything about you yet. Tell me your favourite driver or your "
            "F1 Fantasy team (or send a screenshot of it) and I'll keep it in mind."
        )
        return
    await safe_reply(update.message, f"Here's what I remember about you:\n{facts}\n\n/me clear to forget it all.",
                     parse_mode=None)
