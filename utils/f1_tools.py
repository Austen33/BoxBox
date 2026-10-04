"""Tools the model can call while answering: official data, McLaren analysis,
web search, FIA stewards' documents and per-user memory.

The model picks what it needs for each question, so there are no keyword
lists deciding which data to fetch. Every tool returns plain text.
"""

import asyncio
import logging

from utils import fia, mclaren, userprefs
from utils.f1_data import (
    get_constructor_standings,
    get_current_season,
    get_driver_standings,
    get_qualifying_results,
    resolve_round,
)
from utils.groq_client import Tool
from utils.tavily_client import format_search_results, search

logger = logging.getLogger(__name__)

_YEAR = {"type": ["integer", "null"], "description": "Season, e.g. 2008. Null for the current season."}
_RACE = {
    "type": ["string", "null"],
    "description": "Race, circuit, city or country, e.g. 'Monaco' or 'Baku'. Null for the most recent one.",
}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props),
            "additionalProperties": False}


async def _standings(kind: str = "drivers", year: int | None = None) -> str:
    out = []
    if kind in ("drivers", "both"):
        d = await get_driver_standings(year)
        if d and "error" not in d:
            out.append(f"{d['year']} Driver Championship (after round {d['round']}):\n" + "\n".join(
                f"P{x['position']}: {x['driver']} ({x['team']}) - {x['points']} pts, {x['wins']} wins"
                for x in d["drivers"]))
    if kind in ("constructors", "both"):
        c = await get_constructor_standings(year)
        if c and "error" not in c:
            out.append(f"{c['year']} Constructors' Championship (after round {c['round']}):\n" + "\n".join(
                f"P{x['position']}: {x['team']} - {x['points']} pts" for x in c["constructors"]))
    return "\n\n".join(out) or "Standings unavailable."


async def _round(year: int, race: str | None) -> int | None:
    if race:
        return await resolve_round(year, race)
    if year == get_current_season():
        wk = await mclaren.current_weekend(year)
        return int(wk["round"]) if wk else None
    return None


async def _race_result(year: int | None = None, race: str | None = None, session: str = "race") -> str:
    year = year or get_current_season()
    rnd = await _round(year, race) if (race or session == "sprint") else None
    if race and rnd is None:
        return f"No {year} race matching {race!r}."
    if session == "sprint":
        path, key = f"{year}/{rnd}/sprint.json", "SprintResults"
    else:
        path, key = f"{year}/{rnd or 'last'}/results.json", "Results"
    races = await mclaren._races(path, "Races")
    if not races or not races[0].get(key):
        return (f"No official {session} result published for {race or 'that weekend'} {year} yet. For "
                "this weekend, the FIA's provisional classification (fia_documents) or get_this_weekend "
                "may already have it.")
    r = races[0]
    lines = [f"{r['raceName']} {year} (round {r['round']}), {session} classification:"]
    for x in r[key]:
        d = x["Driver"]
        gap = (x.get("Time") or {}).get("time") or x.get("status", "")
        lines.append(
            f"{mclaren._fmt_pos(x['positionText'])} {d['givenName']} {d['familyName']} "
            f"({x['Constructor']['name']}), grid {x.get('grid', '?')}, {gap}, {x.get('points', 0)} pts"
        )
    return "\n".join(lines)


async def _qualifying(year: int | None = None, race: str | None = None) -> str:
    year = year or get_current_season()
    rnd = await _round(year, race)
    if race and rnd is None:
        return f"No {year} race matching {race!r}."
    q = await mclaren.qualifying_results(year, rnd) if rnd else None
    if not q:  # FastF1 live timing fills the gap before the official result is published
        q = await asyncio.to_thread(get_qualifying_results, year, rnd)
    if not q or "error" in q:
        return (f"No qualifying result for {race or 'the latest weekend'} {year} yet. For this weekend, "
                "try get_this_weekend or the FIA's qualifying classification (fia_documents).")
    lines = [f"Qualifying, {q['name']} {q['year']}:"]
    for x in q["results"]:
        t = (x.get("q3") or "").strip()
        t = f" - {t}" if t and t not in ("nan", "NaT", "None") else ""
        lines.append(f"P{x['position']}: {x['driver']} ({x['team']}){t}")
    return "\n".join(lines)


async def _mclaren(topic: str) -> str:
    if topic == "teammates":
        return await mclaren.teammates_text()
    if topic == "title":
        return await mclaren.title_text()
    if topic == "debrief":
        _, text = await mclaren.debrief_text()
        return text or "No race to debrief yet."
    if topic == "pace":
        from utils.pace import pace_report
        completed = sorted(await mclaren.get_completed_rounds())
        if not completed:
            return "No races run yet."
        return await asyncio.to_thread(pace_report, completed, 3) or "Pace data unavailable."
    return f"Unknown topic {topic!r}."


async def _weekend() -> str:
    return await mclaren.weekend_text() or "No race weekend underway."


async def _web_search(query: str) -> str:
    results = await search(query, max_results=6)
    trimmed = [{**r, "content": (r.get("content") or "")[:700]} for r in results]
    return format_search_results(trimmed)


async def _fia_list(filter: str | None = None) -> str:
    return fia.format_list(await fia.list_documents(), filter or "")


async def _fia_read(doc_numbers: list[int]) -> str:
    return await fia.read_documents(doc_numbers)


DATA_TOOLS = [
    Tool("get_standings", "Driver and/or constructor championship standings for any season.",
         _obj({"kind": {"type": "string", "enum": ["drivers", "constructors", "both"]}, "year": _YEAR}),
         _standings),
    Tool("get_race_result",
         "Full race or sprint classification with grid slot, gap or retirement reason, and points. "
         "Any season back to 1950.",
         _obj({"year": _YEAR, "race": _RACE, "session": {"type": "string", "enum": ["race", "sprint"]}}),
         _race_result),
    Tool("get_qualifying", "Qualifying classification for a race weekend, any season.",
         _obj({"year": _YEAR, "race": _RACE}), _qualifying),
    Tool("get_this_weekend",
         "The current race weekend: sessions run so far with results, and the provisional grid "
         "with penalties applied.",
         _obj({}), _weekend),
    Tool("mclaren_analysis",
         "Computed McLaren analysis for the current season. teammates: Norris vs Piastri head-to-head. "
         "title: championship maths. debrief: the last race for McLaren. pace: race pace vs rivals "
         "over recent rounds (upgrade watch).",
         _obj({"topic": {"type": "string", "enum": ["teammates", "title", "debrief", "pace"]}}),
         _mclaren),
    Tool("web_search",
         "Search F1 news sites (formula1.com, the-race.com, autosport, fia.com and others). For news, "
         "transfers, rumours, upgrades, quotes and anything the data tools don't cover. Write a short, "
         "specific query; you may search again with a different query.",
         _obj({"query": {"type": "string"}}), _web_search),
    Tool("fia_documents",
         "List the official FIA documents for the current or latest race weekend: stewards' decisions, "
         "infringements, summons, classifications, starting grids. Car numbers appear in titles.",
         _obj({"filter": {"type": ["string", "null"],
                          "description": "Words the title must contain, e.g. 'infringement' or 'car 4'."}}),
         _fia_list),
    Tool("read_fia_documents",
         "Read FIA documents (by Doc number from fia_documents): returns a faithful summary of each, "
         "with the offence, penalty and stewards' reasoning, or every row of a classification or grid.",
         _obj({"doc_numbers": {"type": "array", "items": {"type": "integer"}, "maxItems": 10}}),
         _fia_read),
]


def user_tools(user_id: int) -> list[Tool]:
    async def remember(key: str, value: str) -> str:
        return userprefs.set_fact(user_id, key, value)

    return [Tool(
        "remember_about_user",
        "Save a lasting fact about this user for future chats: favourite driver or team, their F1 "
        "Fantasy team, how they like answers. Use a short snake_case key (favourite_driver, "
        "fantasy_team, ...); saving an existing key replaces it, an empty value deletes it.",
        _obj({"key": {"type": "string"}, "value": {"type": "string"}}),
        remember,
    )]


def f1_tools(user_id: int | None = None) -> list[Tool]:
    return DATA_TOOLS + (user_tools(user_id) if user_id else [])


TOOL_RULES = """Tools and accuracy (for chat questions):
- For anything about the race weekend underway, start with get_this_weekend. Results take a few hours to reach the official results feed; until then the FIA's provisional classification documents have them (read them with read_fia_documents).
- Use the data tools for any current-season fact: standings, race, sprint and qualifying results, this weekend's sessions and grid, McLaren analysis, and FIA stewards' decisions. They also cover past seasons. Call several tools in one go when a question needs more than one.
- Use web_search for news, transfers, rumours, upgrades and quotes. Skip it for history, rules and technical questions you can answer yourself.
- For penalties and stewards' decisions, prefer the FIA documents over news reports.
- For current-season race or qualifying results, only state results that came from a tool or the LIVE DATA block. If a tool has nothing for that session, say you don't have it. Never produce results from memory or infer them from headlines.
- Priority when sources disagree: tool data, then LIVE DATA, then search results, then your own knowledge.
- Present race and qualifying results as a short list (P1, P2, P3 with driver and team).
- If the user tells you something lasting about themselves (favourite driver or team, their F1 Fantasy team, how they like answers), save it with remember_about_user and don't mention that you did. Don't save one-off questions.
- Off-topic messages get the one-sentence F1-only reply, with no tool calls."""
