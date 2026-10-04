from telegram import Update
from telegram.ext import ContextTypes

from utils import fia
from utils.f1_tools import DATA_TOOLS
from utils.groq_client import SMART_MODEL, chat
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import stream_reply

_DEFAULT = (
    "Summarise every penalty and stewards' decision from this weekend so far, McLaren first. "
    "Skip summonses that were only superseded by a decision, and say when an incident got no further action."
)

# Titles of documents that carry stewards' business (vs timetables, classifications, notes).
_STEWARDS_WORDS = ("infringement", "decision", "offence", "summons", "penalty", "protest", "right of review")
_MAX_DOCS = 30

_ONE_SHOT_RULES = """The official FIA documents are summarised below. Give each decision as a short line:
driver (car), offence, penalty, one-clause reason. Group deleted lap times into one line per driver at most.
Mention pending summonses that have no decision yet. Never state a penalty that isn't in a document."""

_RULES = """Answer from the official FIA documents: list them with fia_documents, then read the relevant
ones with read_fia_documents (several at once is fine). Car numbers are in the titles; check who drives
which car with the data tools if needed. Give each decision as a short line: driver (car), offence,
penalty, one-clause reason. Never state a penalty that isn't in a document."""


async def stewards_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stewards [question]: penalties and decisions, straight from the FIA documents."""
    if is_rate_limited(update.effective_user.id):
        await update.message.reply_text("Slow down, one question at a time.")
        return
    await update.message.reply_chat_action("typing")

    data = await fia.list_documents()
    if not data["docs"]:
        await update.message.reply_text("Couldn't reach the FIA documents page right now. Try again in a bit.")
        return

    if not context.args:
        # The usual case: hand over the cached summaries of every stewards' item in one go,
        # instead of a tool loop that resends them on each round.
        docs = [d for d in data["docs"] if any(w in d["title"].lower() for w in _STEWARDS_WORDS)]
        if not docs:
            await update.message.reply_text(f"No stewards' decisions published for the {data['event']} yet.")
            return
        prompt = (f"Event: {data['event']}.\n{_DEFAULT}\n\n{_ONE_SHOT_RULES}\n\n"
                  + await fia.summaries_text(docs[:_MAX_DOCS]))
        await stream_reply(update.message, lambda on_text: chat(
            messages=[{"role": "user", "content": prompt}], model=SMART_MODEL, on_text=on_text,
        ))
        return

    prompt = f"Event: {data['event']}.\n{' '.join(context.args)}\n\n{_RULES}"
    await stream_reply(update.message, lambda on_text: chat(
        messages=[{"role": "user", "content": prompt}],
        model=SMART_MODEL,
        tools=DATA_TOOLS,
        on_text=on_text,
    ))
