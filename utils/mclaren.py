"""McLaren-focused data layer built on the Jolpi (Ergast-compatible) API.

Every function returns plain text blocks (or small dicts) that the handlers and
the LLM prompts can use directly. All maths (points gaps, title scenarios,
head-to-heads) is done here in code so the model only has to narrate it.
"""

import asyncio
import logging
from datetime import datetime, timezone

from utils.f1_data import JOLPI_BASE, get_current_season
from utils.http import get_json

logger = logging.getLogger(__name__)

TEAM_ID = "mclaren"
TEAM_NAME = "McLaren"

_TTL = 600  # results change only on race weekends

# Points available in a single weekend.
MAX_DRIVER_RACE = 25
MAX_DRIVER_SPRINT = 8
MAX_TEAM_RACE = 43      # 25 + 18
MAX_TEAM_SPRINT = 15    # 8 + 7


# --------------------------------------------------------------------------
# Raw fetchers
# --------------------------------------------------------------------------

# Jolpi rate-limits bursts (HTTP 429), so cap concurrency and retry once.
_sem = asyncio.Semaphore(3)


async def _get(url: str, ttl: float = _TTL) -> dict | None:
    async with _sem:
        data = await get_json(url, ttl_seconds=ttl)
        if data is None:
            await asyncio.sleep(1.5)
            data = await get_json(url, ttl_seconds=ttl)
        return data


async def _races(path: str, key: str) -> list[dict]:
    """Fetch ``path`` and return MRData.RaceTable.Races, or [] on failure."""
    data = await _get(f"{JOLPI_BASE}/{path}")
    if not data:
        return []
    try:
        return data["MRData"]["RaceTable"]["Races"]
    except (KeyError, TypeError):
        return []


async def get_schedule(year: int | None = None) -> list[dict]:
    year = year or get_current_season()
    return await _races(f"{year}.json?limit=40", "Races")


async def get_completed_rounds(year: int | None = None) -> set[int]:
    """Rounds that already have a published race result."""
    year = year or get_current_season()
    races = await _races(f"{year}/results/1.json?limit=40", "Races")
    return {int(r["round"]) for r in races}


async def get_mclaren_drivers(year: int | None = None) -> list[dict]:
    year = year or get_current_season()
    # Use the standings (race drivers only); the constructor driver list also
    # includes reserves and practice drivers.
    return [
        {"id": d["id"], "code": d["code"], "name": d["name"], "surname": d["surname"]}
        for d in await get_driver_table(year)
        if d["team"] == TEAM_NAME
    ]


async def _standings(kind: str, year: int) -> tuple[int, list[dict]]:
    data = await _get(f"{JOLPI_BASE}/{year}/{kind}.json?limit=40")
    try:
        lst = data["MRData"]["StandingsTable"]["StandingsLists"][0]
        key = "DriverStandings" if kind == "driverStandings" else "ConstructorStandings"
        return int(lst["round"]), lst[key]
    except (KeyError, IndexError, TypeError):
        return 0, []


async def get_driver_table(year: int | None = None) -> list[dict]:
    year = year or get_current_season()
    _, rows = await _standings("driverStandings", year)
    out = []
    for e in rows:
        d = e["Driver"]
        out.append({
            "id": d["driverId"],
            "position": int(e["position"]),
            "name": f"{d['givenName']} {d['familyName']}",
            "surname": d["familyName"],
            "code": d.get("code", ""),
            "team": e["Constructors"][0]["name"] if e.get("Constructors") else "?",
            "points": float(e["points"]),
            "wins": int(e["wins"]),
        })
    return out


async def get_team_table(year: int | None = None) -> list[dict]:
    year = year or get_current_season()
    _, rows = await _standings("constructorStandings", year)
    return [
        {
            "position": int(e["position"]),
            "team": e["Constructor"]["name"],
            "id": e["Constructor"]["constructorId"],
            "points": float(e["points"]),
            "wins": int(e["wins"]),
        }
        for e in rows
    ]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _is_classified(position_text: str) -> bool:
    return position_text.isdigit()


def _fmt_pos(position_text: str) -> str:
    return f"P{position_text}" if position_text.isdigit() else position_text


def _short_race(name: str) -> str:
    return name.replace(" Grand Prix", " GP")


def _as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


async def _driver_season(driver_id: str, year: int) -> dict:
    """Race, sprint and qualifying results for one driver, keyed by round."""
    race_r, sprint_r, quali_r = await asyncio.gather(
        _races(f"{year}/drivers/{driver_id}/results.json?limit=40", "Races"),
        _races(f"{year}/drivers/{driver_id}/sprint.json?limit=40", "Races"),
        _races(f"{year}/drivers/{driver_id}/qualifying.json?limit=40", "Races"),
    )
    races, sprints, qualis = {}, {}, {}
    for r in race_r:
        x = r["Results"][0]
        races[int(r["round"])] = {
            "race": r["raceName"],
            "grid": _as_int(x.get("grid")),
            "pos_text": x.get("positionText", ""),
            "pos": _as_int(x.get("position")),
            "points": float(x.get("points", 0)),
            "status": x.get("status", ""),
        }
    for r in sprint_r:
        x = r["SprintResults"][0]
        sprints[int(r["round"])] = {
            "pos_text": x.get("positionText", ""),
            "pos": _as_int(x.get("position")),
            "points": float(x.get("points", 0)),
        }
    for r in quali_r:
        x = r["QualifyingResults"][0]
        qualis[int(r["round"])] = _as_int(x.get("position"))
    return {"races": races, "sprints": sprints, "qualis": qualis}


# --------------------------------------------------------------------------
# Live snapshot (auto-injected into the system prompt)
# --------------------------------------------------------------------------

async def live_snapshot() -> str:
    """Compact, auto-refreshing standings block. Empty string on failure."""
    try:
        year = get_current_season()
        drivers, teams, completed, schedule = await asyncio.gather(
            get_driver_table(year), get_team_table(year),
            get_completed_rounds(year), get_schedule(year),
        )
        if not drivers or not teams:
            return ""

        lines = [
            f"LIVE {year} DATA (auto-refreshed from the F1 results feed, this overrides the "
            f"static snapshot above wherever they differ). Races completed: {len(completed)}."
        ]
        lines.append("Drivers: " + "; ".join(
            f"{d['position']} {d['name']} ({d['team']}) {d['points']:g}"
            + (f", {d['wins']}W" if d["wins"] else "")
            for d in drivers[:10]
        ))
        mc = [d for d in drivers if d["team"] == TEAM_NAME and d["position"] > 10]
        if mc:
            lines.append("McLaren outside top 10: " + "; ".join(
                f"{d['position']} {d['name']} {d['points']:g}" for d in mc))
        lines.append("Constructors: " + "; ".join(
            f"{t['position']} {t['team']} {t['points']:g}" for t in teams[:11]
        ))
        wins = {}
        for r in await _races(f"{year}/results/1.json?limit=40", "Races"):
            w = r["Results"][0]["Driver"]["familyName"]
            wins[w] = wins.get(w, 0) + 1
        if wins:
            lines.append("Race wins: " + ", ".join(
                f"{k} {v}" for k, v in sorted(wins.items(), key=lambda kv: -kv[1])))

        now = datetime.now(timezone.utc).date().isoformat()
        remaining = [r for r in schedule if int(r["round"]) not in completed and r["date"] >= now]
        if remaining:
            lines.append("Next races: " + ", ".join(
                f"{_short_race(r['raceName'])} {r['date']}" for r in remaining[:4]
            ) + f". {len(remaining)} rounds remain.")
        return "\n".join(lines)
    except Exception:
        logger.warning("live_snapshot failed", exc_info=True)
        return ""


# --------------------------------------------------------------------------
# /teammates
# --------------------------------------------------------------------------

async def teammates_text() -> str:
    """Head-to-head between the two McLaren drivers, computed in code."""
    year = get_current_season()
    mc = await get_mclaren_drivers(year)
    if len(mc) < 2:
        return "McLaren driver data unavailable."
    a, b = mc[0], mc[1]
    sa, sb = await asyncio.gather(_driver_season(a["id"], year), _driver_season(b["id"], year))
    table = await get_driver_table(year)
    pos = {d["code"]: d for d in table}

    def agg(s: dict) -> dict:
        races = s["races"]
        fin = [r for r in races.values() if _is_classified(r["pos_text"])]
        return {
            "points": sum(r["points"] for r in races.values()) + sum(x["points"] for x in s["sprints"].values()),
            "wins": sum(1 for r in races.values() if r["pos"] == 1),
            "podiums": sum(1 for r in races.values() if r["pos"] in (1, 2, 3)),
            "dnfs": sum(1 for r in races.values() if not _is_classified(r["pos_text"]) and r["pos_text"] != "W"),
            "dns": sum(1 for r in races.values() if r["pos_text"] == "W"),
            "avg_finish": sum(r["pos"] for r in fin) / len(fin) if fin else 0,
            "avg_grid": (lambda g: sum(g) / len(g) if g else 0)(
                [r["grid"] for r in races.values() if r["grid"] > 0]),
            "poles": sum(1 for r in races.values() if r["grid"] == 1),
        }

    ga, gb = agg(sa), agg(sb)

    both_q = [r for r in sa["qualis"] if r in sb["qualis"]]
    q_a = sum(1 for r in both_q if sa["qualis"][r] < sb["qualis"][r])
    q_b = len(both_q) - q_a

    both_r = [
        r for r in sa["races"]
        if r in sb["races"]
        and _is_classified(sa["races"][r]["pos_text"]) and _is_classified(sb["races"][r]["pos_text"])
    ]
    r_a = sum(1 for r in both_r if sa["races"][r]["pos"] < sb["races"][r]["pos"])
    r_b = len(both_r) - r_a

    lines = [f"McLaren team-mate battle, {year}, after {len(sa['races'])} races:"]
    for drv, g in ((a, ga), (b, gb)):
        t = pos.get(drv["code"], {})
        lines.append(
            f"{drv['name']}: P{t.get('position', '?')}, {t.get('points', g['points']):g} pts, "
            f"{g['wins']} wins, {g['podiums']} podiums, {g['poles']} poles, "
            f"avg grid {g['avg_grid']:.1f}, avg finish {g['avg_finish']:.1f} (classified races), "
            f"{g['dnfs']} DNFs, {g['dns']} DNS/withdrawn"
        )
    lines.append(
        f"Qualifying head-to-head (grand prix qualifying only, {len(both_q)} sessions): "
        f"{a['surname']} {q_a}, {b['surname']} {q_b}"
    )
    lines.append(
        f"Race head-to-head (rounds where both were classified, {len(both_r)} races): "
        f"{a['surname']} {r_a}, {b['surname']} {r_b}"
    )
    gap = pos.get(a["code"], {}).get("points", 0) - pos.get(b["code"], {}).get("points", 0)
    if gap:
        ahead, behind = (a, b) if gap > 0 else (b, a)
        lines.append(f"Points gap: {ahead['surname']} is {abs(gap):g} pts ahead of {behind['surname']}")

    # Round by round, most recent first (last 5), so the narrative is traceable.
    recent = sorted(set(sa["races"]) & set(sb["races"]), reverse=True)[:5]
    if recent:
        lines.append("Last races (grid>finish):")
        for r in sorted(recent):
            ra, rb = sa["races"][r], sb["races"][r]
            lines.append(
                f"  {_short_race(ra['race'])}: {a['code']} P{ra['grid']}>{_fmt_pos(ra['pos_text'])}, "
                f"{b['code']} P{rb['grid']}>{_fmt_pos(rb['pos_text'])}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# /title
# --------------------------------------------------------------------------

async def title_text() -> str:
    """Championship maths for both titles, computed in code."""
    year = get_current_season()
    drivers, teams, completed, schedule = await asyncio.gather(
        get_driver_table(year), get_team_table(year),
        get_completed_rounds(year), get_schedule(year),
    )
    if not drivers or not teams:
        return "Standings data unavailable."

    remaining = [r for r in schedule if int(r["round"]) not in completed]
    n_rem = len(remaining)
    n_sprint = sum(1 for r in remaining if "Sprint" in r)
    n_done = len(completed)
    if n_rem == 0:
        return f"The {year} season is complete. Final standings are in the standings data."

    max_d = n_rem * MAX_DRIVER_RACE + n_sprint * MAX_DRIVER_SPRINT
    max_t = n_rem * MAX_TEAM_RACE + n_sprint * MAX_TEAM_SPRINT

    lines = [
        f"{year} title maths. {n_done} races done, {n_rem} remaining "
        f"(sprint weekends among them: {n_sprint}). "
        f"Max a driver can score from here: {max_d}. Max a team can score: {max_t}.",
    ]

    leader = drivers[0]
    lines.append(f"DRIVERS: {leader['name']} leads on {leader['points']:g}.")
    alive = [d for d in drivers if d["points"] + max_d >= leader["points"]]
    lines.append(
        "Still mathematically alive: " + ", ".join(d["surname"] for d in alive)
        + (". Everyone else is out." if len(alive) < len(drivers) else ".")
    )
    second = drivers[1]
    lead = leader["points"] - second["points"]
    lines.append(
        f"Lead over P2 ({second['surname']}): {lead:g}. "
        + (f"Title can be clinched once the lead exceeds {max_d} points available to P2 "
           f"(currently {max_d - lead:g} short)." if lead < max_d
           else "The lead already exceeds every point available, so the title is mathematically decided.")
    )

    lead_avg = leader["points"] / n_done if n_done else 0
    projected = leader["points"] + lead_avg * n_rem
    for d in drivers:
        if d["team"] != TEAM_NAME:
            continue
        gap = leader["points"] - d["points"]
        if d["points"] + max_d < leader["points"]:
            lines.append(f"{d['name']}: P{d['position']}, {d['points']:g} pts, {gap:g} behind. "
                         f"Mathematically out of the title fight.")
            continue
        need_avg = (projected - d["points"]) / n_rem
        lines.append(
            f"{d['name']}: P{d['position']}, {d['points']:g} pts, {gap:g} behind. "
            f"If the leader keeps scoring {lead_avg:.1f}/race he finishes on about {projected:.0f}; "
            f"{d['surname']} would need to average about {need_avg:.1f} pts per remaining race "
            f"(the max is {max_d / n_rem:.1f}). This is a projection, not a prediction."
        )

    t_leader = teams[0]
    lines.append(f"CONSTRUCTORS: {t_leader['team']} lead on {t_leader['points']:g}.")
    for i, t in enumerate(teams):
        if t["team"] != TEAM_NAME:
            continue
        lines.append(f"McLaren: P{t['position']}, {t['points']:g} pts.")
        if i > 0:
            ahead = teams[i - 1]
            lines.append(
                f"  Gap to {ahead['team']} (P{ahead['position']}): {ahead['points'] - t['points']:g} "
                f"({max_t} available to them from here)."
            )
        if i + 1 < len(teams):
            behind = teams[i + 1]
            lines.append(
                f"  Lead over {behind['team']} (P{behind['position']}): {t['points'] - behind['points']:g}."
            )
        if t["position"] > 1:
            lines.append(
                f"  Gap to the top: {t_leader['points'] - t['points']:g}. "
                + ("Mathematically out of the constructors' title."
                   if t["points"] + max_t < t_leader["points"]
                   else "Still mathematically alive for the constructors' title.")
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# /debrief
# --------------------------------------------------------------------------

async def debrief_text() -> tuple[str, str]:
    """Return (race_name, data text) for the latest completed race, McLaren-first."""
    year = get_current_season()
    races = await _races(f"{year}/last/results.json", "Races")
    if not races:
        return "", ""
    race = races[0]
    rnd = int(race["round"])
    quali = await _races(f"{year}/{rnd}/qualifying.json", "Races")
    sprint = await _races(f"{year}/{rnd}/sprint.json", "Races")
    drivers, teams = await asyncio.gather(get_driver_table(year), get_team_table(year))

    name = race["raceName"]
    lines = [f"{name} {year} (round {rnd}), date {race['date']}."]

    results = race["Results"]
    lines.append("Top 10: " + "; ".join(
        f"P{r['position']} {r['Driver']['familyName']} ({r['Constructor']['name']})"
        for r in results[:10]
    ))
    if results:
        w = results[0]
        gap2 = results[1].get("Time", {}).get("time", "") if len(results) > 1 else ""
        lines.append(f"Winner: {w['Driver']['givenName']} {w['Driver']['familyName']} for "
                     f"{w['Constructor']['name']}" + (f", P2 finished {gap2} behind." if gap2 else "."))

    mc = [r for r in results if r["Constructor"]["constructorId"] == TEAM_ID]
    q_by_code = {}
    if quali:
        for q in quali[0]["QualifyingResults"]:
            q_by_code[q["Driver"]["code"]] = q
    if quali:
        lines.append("Qualifying top 5: " + "; ".join(
            f"P{q['position']} {q['Driver']['familyName']}" for q in quali[0]["QualifyingResults"][:5]))
    for r in mc:
        code = r["Driver"]["code"]
        q = q_by_code.get(code, {})
        lines.append(
            f"McLaren {r['Driver']['givenName']} {r['Driver']['familyName']}: "
            f"qualified P{q.get('position', '?')}, started P{r['grid']}, "
            f"finished {_fmt_pos(r['positionText'])}, +{r['points']} pts, status: {r['status']}"
            + (f", {r['laps']} laps" if r.get("laps") else "")
        )
    if not mc:
        lines.append("No McLaren results found for this race.")
    if sprint:
        sp = sprint[0]["SprintResults"]
        lines.append("Sprint: " + "; ".join(
            f"{_fmt_pos(x['positionText'])} {x['Driver']['familyName']}" for x in sp[:3]
        ) + ". McLaren: " + (", ".join(
            f"{x['Driver']['familyName']} {_fmt_pos(x['positionText'])}"
            for x in sp if x["Constructor"]["constructorId"] == TEAM_ID) or "none"))

    others = [r for r in results if r["Constructor"]["constructorId"] != TEAM_ID
              and not _is_classified(r["positionText"])]
    if others:
        lines.append("Other retirements: " + "; ".join(
            f"{r['Driver']['familyName']} ({r['status']})" for r in others[:6]))

    team_pts = sum(float(r["points"]) for r in mc)
    lines.append(f"McLaren scored {team_pts:g} points in the race.")
    for d in drivers:
        if d["team"] == TEAM_NAME:
            lines.append(f"Championship now: {d['name']} P{d['position']} on {d['points']:g}.")
    for t in teams:
        if t["team"] == TEAM_NAME:
            idx = t["position"] - 1
            extra = ""
            if idx > 0:
                extra += f", {teams[idx - 1]['points'] - t['points']:g} behind {teams[idx - 1]['team']}"
            if idx + 1 < len(teams):
                extra += f", {t['points'] - teams[idx + 1]['points']:g} ahead of {teams[idx + 1]['team']}"
            lines.append(f"Constructors now: McLaren P{t['position']} on {t['points']:g}{extra}.")
    return name, "\n".join(lines)


# --------------------------------------------------------------------------
# Alerts (scheduler)
# --------------------------------------------------------------------------

async def latest_session_markers() -> dict:
    """Which McLaren-relevant sessions have published results.

    Returns {"race": round|0, "quali": round|0, "sprint": round|0} where each
    value is the highest round that has results for that session type.
    """
    year = get_current_season()
    race, quali, sprint = await asyncio.gather(
        _races(f"{year}/last/results.json", "Races"),
        _races(f"{year}/last/qualifying.json", "Races"),
        _races(f"{year}/last/sprint.json", "Races"),
    )
    return {
        "race": int(race[0]["round"]) if race else 0,
        "quali": int(quali[0]["round"]) if quali else 0,
        "sprint": int(sprint[0]["round"]) if sprint else 0,
    }


async def quali_alert_text() -> tuple[str, str]:
    """(race name, McLaren-first data text) for the latest qualifying."""
    year = get_current_season()
    races = await _races(f"{year}/last/qualifying.json", "Races")
    if not races:
        return "", ""
    q = races[0]["QualifyingResults"]
    lines = [f"{races[0]['raceName']} qualifying: " + "; ".join(
        f"P{x['position']} {x['Driver']['familyName']}" for x in q[:5])]
    for x in q:
        if x["Constructor"]["constructorId"] == TEAM_ID:
            best = x.get("Q3") or x.get("Q2") or x.get("Q1") or ""
            lines.append(f"McLaren {x['Driver']['familyName']}: P{x['position']} ({best})")
    return races[0]["raceName"], "\n".join(lines)
