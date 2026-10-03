"""Race-pace comparison from FastF1 lap data (McLaren vs the field).

For each recent race we take every team's representative race pace: the median
of clean laps (green flag, on track, not in/out laps, within 107% of that
driver's best lap, so safety cars and traffic mess is filtered out). McLaren's
gap to the fastest team and to the key rivals is tracked race by race so
upgrade effects show up as a trend.
"""

import logging

import fastf1
import pandas as pd

from utils import store
from utils.f1_data import get_current_season, JOLPI_BASE
from utils.mclaren import TEAM_NAME

logger = logging.getLogger(__name__)

_CACHE_KEY = "pace_cache_v1"


def _team_pace(year: int, rnd: int) -> dict | None:
    """Median clean-lap time per team for one race. Slow (FastF1 load); run in a thread."""
    session = fastf1.get_session(year, rnd, "R")
    session.load(laps=True, telemetry=False, weather=False, messages=False)
    laps = session.laps
    if laps is None or len(laps) == 0:
        return None

    laps = laps.copy()
    laps = laps[laps["LapTime"].notna()]
    laps = laps[laps["PitInTime"].isna() & laps["PitOutTime"].isna()]
    if "TrackStatus" in laps.columns:
        laps = laps[laps["TrackStatus"].astype(str) == "1"]
    laps = laps[laps["LapNumber"] > 1]
    laps = laps.assign(secs=laps["LapTime"].dt.total_seconds())

    best = laps.groupby("Driver")["secs"].transform("min")
    laps = laps[laps["secs"] <= best * 1.07]

    pace = laps.groupby("Team")["secs"].median().to_dict()
    pace = {k: round(float(v), 3) for k, v in pace.items() if pd.notna(v)}
    if not pace:
        return None
    return {"race": session.event["EventName"], "round": rnd, "pace": pace}


def _pct_gap(pace: dict, team: str, ref: float) -> float:
    return (pace[team] / ref - 1) * 100


def _find_team(pace: dict, name: str) -> str | None:
    for t in pace:
        if name.lower() in t.lower():
            return t
    return None


def pace_report(completed_rounds: list[int], n: int = 3) -> str:
    """Build a text report for the last ``n`` completed rounds (blocking)."""
    year = get_current_season()
    cache = store.load(_CACHE_KEY, {}) or {}
    rows = []
    for rnd in sorted(completed_rounds)[-n:]:
        key = f"{year}-{rnd}"
        if key not in cache:
            try:
                res = _team_pace(year, rnd)
            except Exception as e:
                logger.warning("pace: round %s failed: %s", rnd, e)
                res = None
            if res:
                cache[key] = res
                store.save(_CACHE_KEY, cache)
        if key in cache:
            rows.append(cache[key])

    if not rows:
        return "Race pace data isn't available right now."

    lines = ["McLaren race pace (median clean-lap time per team, traffic and safety cars filtered out):"]
    for r in rows:
        pace = r["pace"]
        mc = _find_team(pace, TEAM_NAME)
        if not mc:
            continue
        fastest_team = min(pace, key=pace.get)
        ref = pace[fastest_team]
        ranked = sorted(pace, key=pace.get)
        pos = ranked.index(mc) + 1
        parts = [f"{r['race']}: McLaren {pace[mc]:.3f}s, {_pct_gap(pace, mc, ref):+.2f}% off the fastest team "
                 f"({fastest_team}), {pos} of {len(ranked)} on pace"]
        for rival in ("Ferrari", "Red Bull", "Mercedes"):
            t = _find_team(pace, rival)
            if t and t != mc:
                parts.append(f"vs {t} {pace[mc] - pace[t]:+.3f}s/lap")
        lines.append("  " + "; ".join(parts))

    if len(rows) >= 2:
        def gap(r):
            mc = _find_team(r["pace"], TEAM_NAME)
            return _pct_gap(r["pace"], mc, min(r["pace"].values())) if mc else None
        first, last = gap(rows[0]), gap(rows[-1])
        if first is not None and last is not None:
            d = last - first
            lines.append(
                f"Trend over these {len(rows)} races: McLaren's gap to the fastest team went from "
                f"{first:+.2f}% to {last:+.2f}% ({'closing' if d < 0 else 'growing'} by {abs(d):.2f} points). "
                f"Different circuits suit different cars, so treat this as indicative, not exact."
            )
    return "\n".join(lines)
