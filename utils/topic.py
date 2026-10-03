"""Keep BoxBox on topic: only Formula 1 (and closely related motorsport).

Obvious F1 messages pass on keywords for free; anything else gets a one-word
yes/no check from the fast model, with recent conversation so follow-ups like
"and him?" are judged in context. If the check itself fails, the message is
allowed through (the system prompt also tells the model to decline off-topic
questions, so that is the second line of defence).
"""

import logging
import re

logger = logging.getLogger(__name__)

OFF_TOPIC_REPLY = (
    "I'm BoxBox, a Formula 1 bot, so I only answer F1 questions. "
    "Ask me about races, results, drivers, McLaren, strategy or the championship."
)

_F1_TERMS = [
    "f1", "formula 1", "formula one", "grand prix", "gp", "fia", "paddock", "pit", "pitstop",
    "pit stop", "tyre", "tire", "drs", "quali", "qualifying", "pole", "podium", "grid",
    "race", "sprint", "practice", "fp1", "fp2", "fp3", "lap", "safety car", "red flag",
    "constructor", "championship", "standings", "points", "penalty", "steward", "undercut",
    "overcut", "downforce", "power unit", "mclaren", "papaya", "ferrari", "mercedes",
    "red bull", "racing bulls", "alpine", "aston martin", "williams", "haas", "audi",
    "sauber", "cadillac", "norris", "piastri", "lando", "oscar", "verstappen", "hamilton",
    "leclerc", "russell", "antonelli", "alonso", "sainz", "albon", "gasly", "ocon",
    "hadjar", "lawson", "lindblad", "bearman", "hulkenberg", "bortoleto", "stroll",
    "colapinto", "bottas", "perez", "stella", "zak brown", "senna", "prost", "hakkinen",
    "schumacher", "button", "vettel", "monaco", "silverstone", "monza", "spa", "suzuka",
    "sepang", "singapore", "baku", "zandvoort", "interlagos", "las vegas", "abu dhabi",
    "mcl40", "kers", "ers", "mgu", "motorsport", "driver", "team principal",
]
_F1_RE = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in _F1_TERMS) + r")\b", re.IGNORECASE)


async def is_on_topic(text: str, history: list[dict] | None = None) -> bool:
    if _F1_RE.search(text or ""):
        return True
    from utils.groq_client import chat, FAST_MODEL  # local import avoids a cycle

    context = ""
    if history:
        context = "Recent conversation:\n" + "\n".join(
            f"{m['role']}: {m['content'][:200]}" for m in history[-4:]
        ) + "\n\n"
    prompt = (
        f"{context}New message: {text}\n\n"
        "Is the new message about Formula 1 or motorsport, or a follow-up to an F1 conversation, "
        "or a greeting/thanks/question about what this F1 bot can do? Answer only YES or NO."
    )
    try:
        verdict = await chat(
            messages=[{"role": "user", "content": prompt}],
            model=FAST_MODEL,
            system="You classify messages for a Formula 1 chatbot. Reply with YES or NO only.",
        )
        return not verdict.strip().upper().startswith("NO")
    except Exception:
        logger.warning("topic check failed; allowing message", exc_info=True)
        return True
