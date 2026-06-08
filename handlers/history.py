from telegram import Update
from telegram.ext import ContextTypes

from utils.groq_client import chat, SMART_MODEL
from utils.tavily_client import search, format_search_results
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply
from utils.http import get_json

JOLPI_BASE = "https://api.jolpi.ca/ergast/f1"

# Cache TTLs (seconds) for Ergast reads.
CAREER_TTL = 3600       # driver results paging
DRIVERS_TTL = 21600     # driver-id resolution (stable within a season)


def _safe_int(value, default: int) -> int:
    """Best-effort int parse. Ergast can return '', 'R', or other non-numeric values."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _fetch_driver_results_at_circuit(driver_id: str, circuit_id: str, limit: int = 10) -> list[dict]:
    """Fetch historical race results for a driver at a specific circuit."""
    results = []
    try:
        offset = 0
        page_size = 100
        while True:
            url = f"{JOLPI_BASE}/drivers/{driver_id}/results.json?limit={page_size}&offset={offset}"
            data = await get_json(url, ttl_seconds=CAREER_TTL)
            if not data:
                break

            races = data["MRData"]["RaceTable"]["Races"]
            if not races:
                break

            for race in races:
                circuit = race.get("Circuit", {})
                if circuit.get("circuitId", "").lower() != circuit_id.lower():
                    continue
                for result in race.get("Results", []):
                    results.append({
                        "year": race.get("season", "?"),
                        "race_name": race.get("raceName", "?"),
                        "position": result.get("position", "?"),
                        "grid": result.get("grid", "?"),
                        "status": result.get("status", "?"),
                        "points": result.get("points", "0"),
                        "constructor": result.get("Constructor", {}).get("name", "?"),
                    })

            total = int(data["MRData"]["total"])
            offset += page_size
            if offset >= total:
                break

        return results[:limit]
    except Exception:
        return []


async def _fetch_driver_id_by_name(
    name: str, include_historical: bool = True, season: str = "current"
) -> str | None:
    """Resolve driver name to driverId for Ergast API.

    ``season`` scopes the roster lookup (``"current"`` by default, or a year
    like ``"2026"`` to pin it). When ``include_historical`` is False, only that
    season's drivers are matched (the direct Ergast lookup is skipped). Callers
    that need to disambiguate against team names — e.g. ``/follow McLaren``
    should be the team, not the historical driver "Bruce McLaren" — use False.
    """
    try:
        url = f"{JOLPI_BASE}/{season}/drivers.json"
        data = await get_json(url, ttl_seconds=DRIVERS_TTL)
        if not data:
            return None

        drivers = data["MRData"]["DriverTable"]["Drivers"]
        name_lower = name.lower().strip()

        # Exact matches (3-letter code or surname) take priority across all
        # drivers, so a query like "VER" can't be stolen by a substring hit in
        # another driver's given name (e.g. "ver" in "Oliver" Bearman).
        for d in drivers:
            if d.get("code", "").lower() == name_lower or d.get("familyName", "").lower() == name_lower:
                return d.get("driverId")

        # Then fall back to a fuzzy full-name contains match.
        for d in drivers:
            full = f"{d.get('givenName', '')} {d.get('familyName', '')}".lower()
            if name_lower in full:
                return d.get("driverId")

        # If not found in current drivers, try a direct (historical) lookup.
        if include_historical:
            url = f"{JOLPI_BASE}/drivers/{name_lower}.json"
            data = await get_json(url, ttl_seconds=DRIVERS_TTL)
            if data and data["MRData"]["DriverTable"]["Drivers"]:
                return data["MRData"]["DriverTable"]["Drivers"][0].get("driverId")

        return None
    except Exception:
        return None


async def _fetch_circuit_id_by_name(name: str) -> str | None:
    """Resolve circuit name to circuitId for Ergast API."""
    circuit_map = {
        "monaco": "monaco",
        "monza": "monza",
        "silverstone": "silverstone",
        "spa": "spa",
        "francorchamps": "spa",
        "suzuka": "suzuka",
        "interlagos": "interlagos",
        "brazil": "interlagos",
        "austin": "americas",
        "americas": "americas",
        "bahrain": "bahrain",
        "sakhir": "bahrain",
        "barcelona": "catalunya",
        "catalunya": "catalunya",
        "hungaroring": "hungaroring",
        "hungary": "hungaroring",
        "red bull ring": "red_bull_ring",
        "spielberg": "red_bull_ring",
        "austria": "red_bull_ring",
        "sochi": "sochi",
        "istanbul": "istanbul",
        "turkey": "istanbul",
        "imola": "imola",
        "mugello": "mugello",
        "portimao": "portimao",
        "jeddah": "jeddah",
        "saudi": "jeddah",
        "miami": "miami",
        "las vegas": "vegas",
        "vegas": "vegas",
        "qatar": "losail",
        "losail": "losail",
        "melbourne": "albert_park",
        "albert park": "albert_park",
        "australia": "albert_park",
        "shanghai": "shanghai",
        "china": "shanghai",
        "marina bay": "marina_bay",
        "singapore": "marina_bay",
        "yas marina": "yas_marina",
        "abu dhabi": "yas_marina",
        "baku": "baku",
        "azerbaijan": "baku",
        "zandvoort": "zandvoort",
        "netherlands": "zandvoort",
        "paul ricard": "paul_ricard",
        "le castellet": "paul_ricard",
        "mexico": "mexico",
        "rodriguez": "mexico",
        "montreal": "montreal",
        "canada": "montreal",
        "nurburgring": "nurburgring",
        "hockenheim": "hockenheim",
        "germany": "hockenheim",
    }

    name_lower = name.lower().strip()
    for key, circuit_id in circuit_map.items():
        if key in name_lower or name_lower in key:
            return circuit_id

    return None


async def _fetch_driver_career_stats(driver_id: str) -> dict | None:
    """Fetch career statistics for a driver."""
    try:
        stats = {
            "total_races": 0,
            "wins": 0,
            "podiums": 0,
            "poles": 0,
            "fastest_laps": 0,
            "dnfs": 0,
            "best_finish": 20,
            "seasons": set(),
            "teams": set(),
        }

        offset = 0
        page_size = 100
        while True:
            url = f"{JOLPI_BASE}/drivers/{driver_id}/results.json?limit={page_size}&offset={offset}"
            data = await get_json(url, ttl_seconds=CAREER_TTL)
            if not data:
                break

            races = data["MRData"]["RaceTable"]["Races"]
            if not races:
                break

            stats["total_races"] += len(races)

            for race in races:
                stats["seasons"].add(race.get("season", "?"))
                for result in race.get("Results", []):
                    pos = _safe_int(result.get("position"), 20)
                    grid = _safe_int(result.get("grid"), 0)
                    status = result.get("status", "")

                    if pos == 1:
                        stats["wins"] += 1
                    if pos <= 3:
                        stats["podiums"] += 1
                    if grid == 1:
                        stats["poles"] += 1
                    if result.get("FastestLap", {}).get("rank") == "1":
                        stats["fastest_laps"] += 1
                    if status != "Finished" and not status.startswith("+"):
                        stats["dnfs"] += 1
                    if pos < stats["best_finish"]:
                        stats["best_finish"] = pos

                    constructor = result.get("Constructor", {}).get("name")
                    if constructor:
                        stats["teams"].add(constructor)

            total = int(data["MRData"]["total"])
            offset += page_size
            if offset >= total:
                break

        stats["seasons"] = sorted(stats["seasons"], reverse=True)
        stats["teams"] = sorted(stats["teams"])
        stats["best_finish"] = stats["best_finish"] if stats["best_finish"] < 20 else "N/A"

        return stats
    except Exception:
        return None


async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history command for driver stats at a circuit."""
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /history [driver] [circuit]\n"
            "Example: /history VER Silverstone\n"
            "Shows driver's past results at a specific circuit."
        )
        return

    driver_input = args[0]
    circuit_input = " ".join(args[1:])

    await update.message.reply_chat_action("typing")

    # Resolve driver and circuit IDs
    driver_id = await _fetch_driver_id_by_name(driver_input)
    circuit_id = await _fetch_circuit_id_by_name(circuit_input)

    if not driver_id:
        await update.message.reply_text(
            f"Couldn't find driver '{driver_input}'. Try the 3-letter code (VER, NOR, LEC) or surname."
        )
        return

    if not circuit_id:
        await update.message.reply_text(
            f"Couldn't identify circuit '{circuit_input}'. Try names like Silverstone, Monaco, Spa, Monza, Suzuka."
        )
        return

    # Fetch historical results
    results = await _fetch_driver_results_at_circuit(driver_id, circuit_id)

    if not results:
        # Search for information
        search_results = await search(f"{driver_input} {circuit_input} F1 history results wins", max_results=5)
        if search_results:
            prompt = f"""User is asking about {driver_input}'s history at {circuit_input}.

Search results:
{format_search_results(search_results)}

Summarize the driver's historical performance at this circuit in 3-4 sentences.
Include wins, podiums, notable moments, and general form."""
            response = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)
            await safe_reply(update.message, response)
            return

        await update.message.reply_text(
            f"No historical data found for {driver_input} at {circuit_input}. "
            f"They may not have raced there in recent seasons."
        )
        return

    # Format results
    text = f"*{driver_input.upper()} at {circuit_input.title()}*\n\n"
    text += "Year | Race | Grid | Finish | Status | Team\n"
    text += "---|---|---|---|---|---\n"

    wins = 0
    podiums = 0
    for r in results:
        pos = r['position']
        if pos == "1":
            wins += 1
            podiums += 1
            pos = "🥇 1"
        elif pos == "2":
            podiums += 1
            pos = "🥈 2"
        elif pos == "3":
            podiums += 1
            pos = "🥉 3"

        text += f"{r['year']} | {r['race_name']} | P{r['grid']} | {pos} | {r['status']} | {r['constructor']}\n"

    summary = f"\n*Summary:* {wins} wins, {podiums} podiums from {len(results)} entries shown"

    prompt = f"""{text}{summary}

Give a 2-3 sentence analysis of this driver's form at this circuit.
What patterns do you see? Any standout performances or struggles?"""

    response = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)
    await safe_reply(update.message, f"{text}{summary}\n\n{response}")


async def career_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /career command for driver career timeline."""
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /career [driver]\n"
            "Example: /career VER\n"
            "Shows driver's career statistics and timeline."
        )
        return

    driver_input = args[0]

    await update.message.reply_chat_action("typing")

    # Resolve driver ID
    driver_id = await _fetch_driver_id_by_name(driver_input)

    if not driver_id:
        await update.message.reply_text(
            f"Couldn't find driver '{driver_input}'. Try the 3-letter code (VER, NOR, LEC, HAM, ALO) or surname."
        )
        return

    # Fetch career stats
    stats = await _fetch_driver_career_stats(driver_id)

    if not stats:
        # Fallback to search
        search_results = await search(f"{driver_input} F1 driver career statistics wins championships", max_results=5)
        if search_results:
            prompt = f"""User is asking about {driver_input}'s F1 career.

Search results:
{format_search_results(search_results)}

Provide a career overview in 4-5 sentences covering:
1. Championship titles and best seasons
2. Total wins and podiums
3. Teams driven for
4. Notable achievements or records"""
            response = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)
            await safe_reply(update.message, response)
            return

        await update.message.reply_text(f"Couldn't fetch career data for {driver_input}.")
        return

    # Format career stats
    text = f"*{driver_input.upper()} — Career Statistics*\n\n"
    text += f"Races: {stats['total_races']}\n"
    text += f"Wins: {stats['wins']}\n"
    text += f"Podiums: {stats['podiums']}\n"
    text += f"Poles: {stats['poles']}\n"
    text += f"Fastest Laps: {stats['fastest_laps']}\n"
    text += f"DNFs: {stats['dnfs']}\n"
    text += f"Best Finish: P{stats['best_finish']}\n\n"
    text += f"Seasons: {', '.join(map(str, stats['seasons'][:10]))}{'...' if len(stats['seasons']) > 10 else ''}\n\n"
    text += f"Teams: {', '.join(stats['teams'])}"

    prompt = f"""{text}

Provide a brief 3-4 sentence career narrative:
1. What type of driver are they based on these stats?
2. Any standout achievements or patterns?
3. Career trajectory (rising, peak, veteran)?"""

    response = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)
    await safe_reply(update.message, f"{text}\n\n{response}")
