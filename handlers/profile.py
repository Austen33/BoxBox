"""/driver and /team profile cards.

A profile is a single composed view over data the bot already knows how to
fetch: the cached Ergast driver-id resolver and career-stats aggregator from
:mod:`handlers.history`, plus the season-standings endpoints. No new data
source — just a denser presentation of existing data, finished with a short
LLM scouting note.
"""

import asyncio
from datetime import date, datetime

from telegram import Update
from telegram.ext import ContextTypes

from utils.f1_data import get_current_season, STANDINGS_TTL
from utils.groq_client import chat, SMART_MODEL
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply
from utils.http import get_json
from handlers.history import (
    JOLPI_BASE,
    DRIVERS_TTL,
    _fetch_driver_id_by_name,
    _fetch_driver_career_stats,
)

# Constructor id/bio resolution is stable within a season.
CONSTRUCTORS_TTL = 21600

# Common spellings/aliases → Ergast constructorId. Falls back to a live
# /current/constructors lookup when the input isn't in here.
_CONSTRUCTOR_ALIASES = {
    "red bull": "red_bull", "redbull": "red_bull", "rbr": "red_bull",
    "racing bulls": "rb", "vcarb": "rb", "rb": "rb",
    "alphatauri": "rb", "toro rosso": "rb",
    "ferrari": "ferrari", "scuderia ferrari": "ferrari",
    "mercedes": "mercedes", "merc": "mercedes",
    "mclaren": "mclaren",
    "aston martin": "aston_martin", "aston": "aston_martin",
    "alpine": "alpine", "renault": "alpine",
    "williams": "williams",
    "haas": "haas",
    "sauber": "sauber", "kick sauber": "sauber", "stake": "sauber",
    "alfa romeo": "sauber",
}


def _age(dob: str) -> int | None:
    try:
        born = datetime.strptime(dob, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


# --- Driver data ---------------------------------------------------------

async def _fetch_driver_bio(driver_id: str) -> dict | None:
    data = await get_json(f"{JOLPI_BASE}/drivers/{driver_id}.json", ttl_seconds=DRIVERS_TTL)
    try:
        return data["MRData"]["DriverTable"]["Drivers"][0]
    except (TypeError, KeyError, IndexError):
        return None


async def _fetch_driver_current_standing(driver_id: str, year: int) -> dict | None:
    url = f"{JOLPI_BASE}/{year}/drivers/{driver_id}/driverStandings.json"
    data = await get_json(url, ttl_seconds=STANDINGS_TTL)
    try:
        lists = data["MRData"]["StandingsTable"]["StandingsLists"]
        if not lists:
            return None
        entry = lists[0]["DriverStandings"][0]
        return {
            "position": entry.get("position", "?"),
            "points": entry.get("points", "0"),
            "wins": entry.get("wins", "0"),
            "team": entry["Constructors"][0]["name"] if entry.get("Constructors") else "Unknown",
        }
    except (TypeError, KeyError, IndexError):
        return None


# --- Constructor data ----------------------------------------------------

async def _fetch_constructor_id_by_name(name: str, season: str = "current") -> str | None:
    name_lower = name.lower().strip()

    data = await get_json(f"{JOLPI_BASE}/{season}/constructors.json", ttl_seconds=CONSTRUCTORS_TTL)
    season_ids: set[str] = set()
    try:
        for c in data["MRData"]["ConstructorTable"]["Constructors"]:
            cid = c.get("constructorId", "")
            cid_lower = cid.lower()
            season_ids.add(cid_lower)
            cname = c.get("name", "").lower()
            if name_lower == cid_lower or name_lower in cname or cname in name_lower:
                return cid
    except (TypeError, KeyError):
        pass

    # Alias map as a fallback, but only honour aliases that point at a team
    # actually competing in this season (so old names don't resolve).
    alias = _CONSTRUCTOR_ALIASES.get(name_lower)
    if alias is None:
        for key, cid in _CONSTRUCTOR_ALIASES.items():
            if key in name_lower or name_lower in key:
                alias = cid
                break
    if alias and (not season_ids or alias in season_ids):
        return alias
    return None


async def _fetch_constructor_bio(constructor_id: str) -> dict | None:
    data = await get_json(f"{JOLPI_BASE}/constructors/{constructor_id}.json", ttl_seconds=CONSTRUCTORS_TTL)
    try:
        return data["MRData"]["ConstructorTable"]["Constructors"][0]
    except (TypeError, KeyError, IndexError):
        return None


async def _fetch_constructor_current_standing(constructor_id: str, year: int) -> dict | None:
    url = f"{JOLPI_BASE}/{year}/constructors/{constructor_id}/constructorStandings.json"
    data = await get_json(url, ttl_seconds=STANDINGS_TTL)
    try:
        lists = data["MRData"]["StandingsTable"]["StandingsLists"]
        if not lists:
            return None
        entry = lists[0]["ConstructorStandings"][0]
        return {
            "position": entry.get("position", "?"),
            "points": entry.get("points", "0"),
            "wins": entry.get("wins", "0"),
        }
    except (TypeError, KeyError, IndexError):
        return None


async def _fetch_constructor_drivers(constructor_id: str, year: int) -> list[str]:
    # Use the last race's *results* (not the drivers endpoint, which also lists
    # reserve / FP1-only drivers) so we get just the current race line-up.
    url = f"{JOLPI_BASE}/{year}/last/constructors/{constructor_id}/results.json"
    data = await get_json(url, ttl_seconds=STANDINGS_TTL)
    try:
        races = data["MRData"]["RaceTable"]["Races"]
        if not races:
            return []
        names = []
        for result in races[0].get("Results", []):
            d = result.get("Driver", {})
            name = f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()
            if name and name not in names:
                names.append(name)
        return names
    except (TypeError, KeyError, IndexError):
        return []


# --- Handlers ------------------------------------------------------------

async def driver_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/driver [name] — season standing + career stats + a short scouting note."""
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /driver [name]\n"
            "Example: /driver VER\n"
            "Shows a profile card: this season's standing plus career stats."
        )
        return

    driver_input = " ".join(args)
    await update.message.reply_chat_action("typing")

    driver_id = await _fetch_driver_id_by_name(driver_input)
    if not driver_id:
        await update.message.reply_text(
            f"Couldn't find driver '{driver_input}'. Try the 3-letter code (VER, NOR, LEC) or surname."
        )
        return

    year = get_current_season()
    bio, standing, stats = await asyncio.gather(
        _fetch_driver_bio(driver_id),
        _fetch_driver_current_standing(driver_id, year),
        _fetch_driver_career_stats(driver_id),
    )

    if not bio:
        await update.message.reply_text(f"Couldn't load a profile for '{driver_input}'.")
        return

    name = f"{bio.get('givenName', '')} {bio.get('familyName', '')}".strip()
    code = bio.get("code", "")
    number = bio.get("permanentNumber", "")
    nationality = bio.get("nationality", "")
    age = _age(bio.get("dateOfBirth", ""))

    header = f"*{name}*"
    if code:
        header += f" ({code})"
    if number:
        header += f" #{number}"

    lines = [header]
    bio_bits = [b for b in (nationality, f"{age} years old" if age is not None else None) if b]
    if bio_bits:
        lines.append(" · ".join(bio_bits))
    lines.append("")

    if standing:
        lines.append(
            f"*{year} season:* P{standing['position']} · "
            f"{standing['points']} pts · {standing['wins']} wins · {standing['team']}"
        )
    else:
        lines.append(f"*{year} season:* no standings entry yet")

    if stats:
        lines.append("")
        lines.append("*Career*")
        lines.append(f"Races: {stats['total_races']}  ·  Wins: {stats['wins']}  ·  Podiums: {stats['podiums']}")
        lines.append(f"Poles: {stats['poles']}  ·  Fastest laps: {stats['fastest_laps']}  ·  DNFs: {stats['dnfs']}")
        lines.append(f"Best finish: P{stats['best_finish']}  ·  Seasons: {len(stats['seasons'])}")
        if stats["teams"]:
            lines.append(f"Teams: {', '.join(stats['teams'])}")

    card = "\n".join(lines)

    prompt = f"""{card}

In 2-3 sentences, give a punchy scouting-report read on this driver: their reputation, current form, and what they're known for. No filler, no preamble — talk like a race engineer to a smart fan."""
    blurb = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)

    await safe_reply(update.message, f"{card}\n\n{blurb}")


async def team_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/team [name] — constructor profile: season standing, line-up, short note."""
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /team [name]\n"
            "Example: /team Ferrari\n"
            "Shows a constructor profile: this season's standing and line-up."
        )
        return

    team_input = " ".join(args)
    await update.message.reply_chat_action("typing")

    constructor_id = await _fetch_constructor_id_by_name(team_input)
    if not constructor_id:
        await update.message.reply_text(
            f"Couldn't find team '{team_input}'. Try names like Ferrari, McLaren, Red Bull, Mercedes."
        )
        return

    year = get_current_season()
    bio, standing, drivers = await asyncio.gather(
        _fetch_constructor_bio(constructor_id),
        _fetch_constructor_current_standing(constructor_id, year),
        _fetch_constructor_drivers(constructor_id, year),
    )

    if not bio:
        await update.message.reply_text(f"Couldn't load a profile for '{team_input}'.")
        return

    name = bio.get("name", team_input.title())
    nationality = bio.get("nationality", "")

    lines = [f"*{name}*"]
    if nationality:
        lines.append(nationality)
    lines.append("")

    if standing:
        lines.append(
            f"*{year} season:* P{standing['position']} · "
            f"{standing['points']} pts · {standing['wins']} wins"
        )
    else:
        lines.append(f"*{year} season:* no standings entry yet")

    if drivers:
        lines.append(f"*Line-up:* {', '.join(drivers)}")

    card = "\n".join(lines)

    prompt = f"""{card}

In 2-3 sentences, give a punchy read on this F1 team's current situation: their form this season, strengths/weaknesses, and trajectory. No filler, no preamble — talk like a race engineer to a smart fan."""
    blurb = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)

    await safe_reply(update.message, f"{card}\n\n{blurb}")
