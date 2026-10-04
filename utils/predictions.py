"""The bot's own /predict podium calls, saved per round and scored after the race."""
import logging

from utils import store

logger = logging.getLogger(__name__)

_KEY = "predictions"
_MAX_KEPT = 30

_SCHEMA = {
    "type": "object",
    "properties": {
        "podium": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Predicted P1, P2, P3 as three-letter driver codes (e.g. NOR), winner first. Empty if no podium was predicted.",
        },
    },
    "required": ["podium"],
    "additionalProperties": False,
}


async def save_from_text(year: int, rnd: int, text: str, drivers: list[str]) -> None:
    """Pull the predicted podium out of a /predict reply and keep it for scoring."""
    from utils.groq_client import chat_json

    codes = ", ".join(drivers) if drivers else "standard three-letter codes"
    data = await chat_json(
        [{"role": "user", "content": f"Driver codes: {codes}\n\nPrediction:\n{text}"}],
        _SCHEMA,
        "podium_prediction",
    )
    podium = [c.strip().upper()[:3] for c in data.get("podium") or [] if c.strip()][:3]
    if len(podium) < 3:
        return
    saved = store.load(_KEY, {}) or {}
    saved[f"{year}-{rnd}"] = podium
    for k in list(saved)[: max(0, len(saved) - _MAX_KEPT)]:
        saved.pop(k, None)
    store.save(_KEY, saved)
    logger.info("Saved /predict podium for %s-%s: %s", year, rnd, podium)


def score_line(year: int, rnd: int, result_codes: list[str]) -> str:
    """One line comparing the saved podium call with the actual top three."""
    podium = (store.load(_KEY, {}) or {}).get(f"{year}-{rnd}")
    if not podium or len(result_codes) < 3:
        return ""
    actual = [c.upper() for c in result_codes[:3]]
    exact = sum(p == a for p, a in zip(podium, actual))
    on_podium = len(set(podium) & set(actual))
    winner = "winner right" if podium[0] == actual[0] else "winner wrong"
    return (
        f"My /predict call: {', '.join(podium)}. {winner.capitalize()}, "
        f"{on_podium} of 3 on the podium, {exact} in the exact spot."
    )
