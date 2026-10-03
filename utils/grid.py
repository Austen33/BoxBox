"""Grid penalties and the provisional starting grid for the current race weekend.

Penalties aren't in any results feed until the race has been run, so during a
race weekend this searches F1 news for penalty reports, has the fast model
extract them as structured data, and applies them to the official qualifying
order to give a provisional starting grid. Cached for a couple of hours and
only active between the first session of the weekend and the race.
"""

import asyncio
import json
import logging
import re
import time

from utils.tavily_client import get_tavily_client, ALLOWED_DOMAINS

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 2 * 3600
BACK_OF_GRID = 99  # sentinel for "start from the back"
_cache: dict = {}  # race name -> {"ts", "penalties", "sources"}
_lock = asyncio.Lock()

_EXTRACT_PROMPT = """From these news snippets about the {race}, list every GRID penalty (places dropped or
back-of-grid / pit-lane start) that applies to THIS race's starting grid. Ignore time penalties, fines,
reprimands, and penalties for other races. Use the driver's 3-letter code from this list: {codes}.

Reply with JSON only, no prose:
{{"penalties": [{{"driver": "HAD", "places": 5, "back_of_grid": false, "pit_lane": false, "reason": "new ICE"}}]}}
If a driver's places add up from several penalties, give the total. Use "back_of_grid": true for
back-of-grid starts or penalties over 15 places. If no grid penalties are reported, return {{"penalties": []}}.

Snippets:
{snippets}"""


async def _search(race_name: str) -> list[dict]:
    client = get_tavily_client()
    queries = [f"{race_name} grid penalty", f"{race_name} starting grid penalties"]
    results = await asyncio.gather(*[
        asyncio.wait_for(
            client.search(query=q, topic="news", days=4, search_depth="basic",
                          max_results=8, include_domains=ALLOWED_DOMAINS),
            timeout=20,
        )
        for q in queries
    ], return_exceptions=True)
    seen, items = set(), []
    for res in results:
        if isinstance(res, Exception):
            continue
        for r in res.get("results", []):
            if r.get("url") not in seen:
                seen.add(r.get("url"))
                items.append(r)
    return items


async def _extract(race_name: str, items: list[dict], codes: list[str]) -> list[dict]:
    from utils.groq_client import chat, FAST_MODEL

    snippets = "\n".join(
        f"- {r.get('title', '')}: {' '.join((r.get('content') or '').split())[:500]}" for r in items[:12]
    )
    raw = await chat(
        messages=[{"role": "user", "content": _EXTRACT_PROMPT.format(
            race=race_name, codes=", ".join(codes), snippets=snippets)}],
        model=FAST_MODEL,
        system="You extract structured data from news. Output valid JSON only.",
    )
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    data = json.loads(m.group(0)) if m else {"penalties": []}
    out = []
    for p in data.get("penalties", []):
        code = str(p.get("driver", "")).upper().strip()
        if code not in codes:
            continue  # never invent drivers
        places = int(p.get("places") or 0)
        back = bool(p.get("back_of_grid")) or bool(p.get("pit_lane")) or places > 15
        out.append({
            "driver": code,
            "places": BACK_OF_GRID if back else places,
            "pit_lane": bool(p.get("pit_lane")),
            "reason": str(p.get("reason", ""))[:80],
        })
    return out


async def penalties(race_name: str, codes: list[str]) -> list[dict]:
    entry = _cache.get(race_name)
    if entry and time.time() - entry["ts"] < REFRESH_SECONDS:
        return entry["penalties"]
    async with _lock:
        entry = _cache.get(race_name)
        if entry and time.time() - entry["ts"] < REFRESH_SECONDS:
            return entry["penalties"]
        try:
            items = await _search(race_name)
            found = await _extract(race_name, items, codes) if items else []
            _cache[race_name] = {"ts": time.time(), "penalties": found}
            return found
        except KeyError:
            return []  # no TAVILY_API_KEY
        except Exception:
            logger.warning("grid penalty lookup failed", exc_info=True)
            # keep serving the last good answer; retry in 15 minutes
            if entry:
                entry["ts"] = time.time() - REFRESH_SECONDS + 900
                return entry["penalties"]
            return []


def apply_penalties(quali_order: list[str], pens: list[dict]) -> list[str]:
    """Approximate the FIA procedure: place-drops applied in qualifying order,
    then back-of-grid starters behind everyone (in qualifying order), pit-lane last."""
    by_driver = {p["driver"]: p for p in pens}
    back = [d for d in quali_order if by_driver.get(d, {}).get("places") == BACK_OF_GRID
            and not by_driver[d]["pit_lane"]]
    pit = [d for d in quali_order if by_driver.get(d, {}).get("pit_lane")]
    grid = [d for d in quali_order if d not in back and d not in pit]
    for d in [d for d in quali_order if d in by_driver and d in grid]:
        places = by_driver[d]["places"]
        if not places:
            continue
        i = grid.index(d)
        grid.pop(i)
        grid.insert(min(i + places, len(grid)), d)
    return grid + back + pit


async def grid_block(race_name: str, quali_rows: list[dict]) -> str:
    """Text for the live snapshot: penalties + provisional starting grid."""
    codes = [r["code"] for r in quali_rows if r.get("code")]
    if not codes:
        return ""
    pens = await penalties(race_name, codes)
    if not pens:
        return ("Grid penalties: none found in news reports so far (the FIA confirms the official "
                "starting grid before the race).")
    desc = "; ".join(
        f"{p['driver']} {'pit-lane start' if p['pit_lane'] else 'back of grid' if p['places'] == BACK_OF_GRID else str(p['places']) + ' places'}"
        + (f" ({p['reason']})" if p["reason"] else "")
        for p in pens
    )
    grid = apply_penalties(codes, pens)
    return (
        f"Grid penalties (reported by F1 news sites): {desc}.\n"
        "Provisional starting grid after penalties (computed from the official qualifying order; "
        "the FIA's official grid can differ slightly): "
        + ", ".join(f"P{i + 1} {d}" for i, d in enumerate(grid))
    )


async def grid_data() -> dict | None:
    """Everything needed to draw this weekend's starting grid.

    Official once the race has been run (grid slots from the race result),
    otherwise provisional: official qualifying order + reported penalties.
    """
    from utils import mclaren, sessions
    from utils.f1_data import get_current_season

    year = get_current_season()
    wk = await mclaren.current_weekend(year)
    if not wk:
        return None
    rnd = int(wk["round"])
    quali, _sprint, race = await mclaren._weekend_sessions(year, rnd)
    meta = await sessions.driver_meta(year, rnd)

    def entry(pos: int, drv: dict, team_fallback: str, note: str = "") -> dict:
        code = drv.get("code") or drv["familyName"][:3].upper()
        m = meta.get(code, {})
        return {
            "pos": pos, "code": code, "name": m.get("name") or drv["familyName"],
            "team": m.get("team") or team_fallback, "color": m.get("color", ""), "note": note,
        }

    base = {"race": wk["raceName"], "round": rnd, "date": wk["date"]}
    if race:
        results = race[0]["Results"]
        on_grid = sorted([r for r in results if int(r.get("grid", 0)) > 0], key=lambda r: int(r["grid"]))
        pit = [r for r in results if int(r.get("grid", 0)) == 0]
        entries = [entry(i + 1, r["Driver"], r["Constructor"]["name"]) for i, r in enumerate(on_grid)]
        entries += [entry(len(entries) + i + 1, r["Driver"], r["Constructor"]["name"], "PIT LANE")
                    for i, r in enumerate(pit)]
        return {**base, "status": "official", "entries": entries,
                "footnote": "Official starting grid from the race classification. "
                            "Team colours from F1 timing data."}
    if not quali:
        return None

    rows = quali[0]["QualifyingResults"]
    by_code = {(r["Driver"].get("code") or r["Driver"]["familyName"][:3].upper()): r for r in rows}
    codes = list(by_code)
    pens = await penalties(wk["raceName"], codes)
    pen_by = {p["driver"]: p for p in pens}
    order = apply_penalties(codes, pens)

    entries = []
    for i, code in enumerate(order):
        r = by_code[code]
        note = ""
        if code in pen_by:
            p = pen_by[code]
            what = "PIT LANE" if p["pit_lane"] else "BACK OF GRID" if p["places"] == BACK_OF_GRID else f"+{p['places']} PEN"
            note = f"{what} · Q{r['position']}"
        entries.append(entry(i + 1, r["Driver"], r["Constructor"]["name"], note))
    pen_text = ", ".join(f"{p['driver']} ({p['reason'] or 'grid penalty'})" for p in pens) or "none reported"
    return {**base, "status": "provisional", "entries": entries, "penalties": pens,
            "footnote": "Provisional: official qualifying order with grid penalties reported by F1 media "
                        f"applied ({pen_text}). The FIA confirms the official grid before the race. "
                        "Team colours from F1 timing data."}
