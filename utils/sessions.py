"""Automatic post-session collection from F1's live-timing feed (via FastF1).

The race calendar gives every session's start time, so after each session ends
the scheduler pulls the classification from F1's official timing data and keeps
it in the store. The live snapshot then includes practice, sprint, qualifying
and race results within minutes, without anyone having to ask.

Positions are worked out from lap data, because the feed's own Position column
is empty until the official results are published:
  - practice: fastest lap per driver
  - qualifying / sprint qualifying: knockout order (Q3 times, then Q2, then Q1)
  - sprint / race: laps completed, then running position on the last lap
Qualifying, sprint and race are labelled provisional; the official Jolpi
classification replaces them once it is published.

Qualifying, sprint and race are also polled every couple of minutes from just
before they could finish, using the tiny SessionStatus feed, so results land
(and get pushed to subscribers) a few minutes after the chequered flag.
"""

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

import fastf1
import pandas as pd
from fastf1 import _api as ff1_api  # SessionStatus feed (fastf1.api is being made private)

import utils.f1_data  # noqa: F401  (enables the FastF1 cache directory)
from utils import store

logger = logging.getLogger(__name__)

_STORE_KEY = "session_results_v1"
_MAX_STORED = 40

# Jolpi schedule key -> (FastF1 session code, display name, typical length in minutes)
SESSIONS = [
    ("FirstPractice", "FP1", "Practice 1", 60),
    ("SecondPractice", "FP2", "Practice 2", 60),
    ("ThirdPractice", "FP3", "Practice 3", 60),
    ("SprintQualifying", "SQ", "Sprint Qualifying", 45),
    ("Sprint", "S", "Sprint", 45),
    ("Qualifying", "Q", "Qualifying", 60),
    ("Race", "R", "Race", 130),
]
_PRACTICE = {"FP1", "FP2", "FP3"}
# Minutes after the scheduled end to try collecting (live-timing files can lag).
_ATTEMPT_OFFSETS = [12, 30, 60, 120, 240]
# Sessions polled right through their likely finish: (first, last) minutes after
# the start. Races run from ~85 minutes to well over 2 hours with red flags.
_FAST_POLL = {"SQ": (35, 100), "S": (25, 100), "Q": (50, 150), "R": (80, 260)}
_POLL_MINUTES = 2
# Only alert subscribers for results collected this long after the scheduled
# end, so a catch-up after downtime doesn't push days-old sessions.
_ALERT_WINDOW = timedelta(hours=4)
_QUALI_LIKE = {"Q", "SQ"}
# race/sprint key -> when "Finished" (chequered flag) was first seen
_flag_seen: dict[str, float] = {}
_inflight: set[str] = set()


def _key(year: int, rnd: int, code: str) -> str:
    return f"{year}-{rnd}-{code}"


def get_stored(year: int, rnd: int) -> dict:
    """All collected sessions for one round, keyed by session code."""
    data = store.load(_STORE_KEY, {}) or {}
    prefix = f"{year}-{rnd}-"
    return {k[len(prefix):]: v for k, v in data.items() if k.startswith(prefix)}


def _save(year: int, rnd: int, code: str, result: dict) -> None:
    data = store.load(_STORE_KEY, {}) or {}
    data[_key(year, rnd, code)] = result
    if len(data) > _MAX_STORED:
        for k in sorted(data, key=lambda k: data[k].get("fetched", 0))[: len(data) - _MAX_STORED]:
            data.pop(k, None)
    store.save(_STORE_KEY, data)


def _fmt_lap(td) -> str:
    if td is None or pd.isna(td):
        return ""
    secs = td.total_seconds()
    return f"{int(secs // 60)}:{secs % 60:06.3f}"


def _best_laps(laps) -> pd.DataFrame:
    valid = laps[laps["LapTime"].notna()]
    best = valid.groupby("Driver").agg(best=("LapTime", "min"), team=("Team", "first"), laps=("LapNumber", "count"))
    return best.sort_values("best")


def _classify(session, code: str) -> list[dict]:
    laps = session.laps
    if code in _PRACTICE:
        best = _best_laps(laps)
        return [
            {"pos": i + 1, "driver": d, "team": r.team, "time": _fmt_lap(r.best), "laps": int(r.laps)}
            for i, (d, r) in enumerate(best.iterrows())
        ]

    if code in ("Q", "SQ"):
        rows, placed = [], set()
        parts = laps.split_qualifying_sessions()
        for part in reversed(parts):  # Q3 first, then Q2, then Q1
            if part is None or len(part) == 0:
                continue
            for d, r in _best_laps(part).iterrows():
                if d not in placed:
                    placed.add(d)
                    rows.append({"pos": len(rows) + 1, "driver": d, "team": r.team, "time": _fmt_lap(r.best)})
        return rows

    # Sprint / race: most laps completed, then running position on the final lap.
    last = laps.sort_values("LapNumber").groupby("Driver").tail(1)
    last = last.assign(pos_key=last["Position"].fillna(99)).sort_values(
        ["LapNumber", "pos_key"], ascending=[False, True]
    )
    leader_laps = int(last["LapNumber"].max()) if len(last) else 0
    return [
        {
            "pos": i + 1,
            "driver": r.Driver,
            "team": r.Team,
            "laps": int(r.LapNumber),
            "note": "" if int(r.LapNumber) >= leader_laps - 1 else f"{leader_laps - int(r.LapNumber)} laps down/retired",
        }
        for i, r in enumerate(last.itertuples())
    ]


def _is_finished(session, key: str, code: str) -> bool:
    """Blocking, uncached: has live timing marked the whole session as over?

    Reads only the small SessionStatus feed, so it's cheap to poll. Each of
    Q1/Q2/Q3 ends with "Finished", so qualifying needs all three (or the final
    "Finalised"/"Ends"). For a race or sprint, "Finished" is the leader taking
    the flag; wait one more poll so the rest of the field crosses the line.
    """
    with fastf1.Cache.disabled():
        try:
            status = ff1_api.session_status_data(session.api_path)
        except Exception as e:
            logger.info("sessions: no status for %s yet (%s)", key, e)
            return False
    seen = [str(s) for s in status["Status"]]
    if {"Finalised", "Ends"} & set(seen):
        return True
    if code in _QUALI_LIKE:
        return seen.count("Finished") >= 3
    if "Finished" not in seen:
        return False
    first = _flag_seen.setdefault(key, time.time())
    return time.time() - first >= 90


def _drop_stale_cache(session) -> None:
    """Forget anything FastF1 cached for this session while it was still running
    (e.g. someone used /lap mid-race), so later loads see the full session."""
    cache = fastf1.Cache
    try:
        if cache._CACHE_DIR:
            shutil.rmtree(os.path.join(cache._CACHE_DIR, session.api_path[8:]), ignore_errors=True)
        http = cache._requests_session_cached
        if http is not None:
            http.cache.delete(urls=[u for u in http.cache.urls() if session.api_path in u])
    except Exception:
        logger.info("sessions: couldn't clear cached data for %s", session.api_path, exc_info=True)


def fetch_session(year: int, rnd: int, code: str) -> dict | None:
    """Blocking: load one session from live timing and classify it. None if not ready.

    Bypasses FastF1's cache: it keeps parsed data forever, so a load during the
    session would otherwise freeze it half-finished.
    """
    session = fastf1.get_session(year, rnd, code)
    if not _is_finished(session, _key(year, rnd, code), code):
        return None
    with fastf1.Cache.disabled():  # not thread-safe, but at worst skips caching elsewhere briefly
        session.load(laps=True, telemetry=False, weather=False, messages=False)
    _drop_stale_cache(session)
    if session.laps is None or len(session.laps) == 0:
        return None
    # Don't store a half-finished session (red flags and delays overrun the
    # scheduled end); try again on the next attempt.
    status = getattr(session, "session_status", None)
    if status is not None and len(status) and "Status" in status:
        if not set(status["Status"].astype(str)) & {"Finished", "Finalised", "Ends"}:
            return None
    rows = _classify(session, code)
    if not rows:
        return None
    return {
        "event": session.event["EventName"],
        "session": session.name,
        "code": code,
        "fetched": time.time(),
        "official": False,
        "rows": rows,
    }


def describe(result: dict, team: str = "McLaren", top: int = 6) -> str:
    """One-line text summary of a collected session, McLaren-first."""
    code = result["code"]
    label = result["session"] + ("" if code in _PRACTICE else " (provisional, from live timing)")

    def cell(r):
        extra = r.get("time") or (r.get("note") or "")
        return f"P{r['pos']} {r['driver']}" + (f" {extra}" if extra else "")

    parts = [f"{label}: " + "; ".join(cell(r) for r in result["rows"][:top])]
    mc = [cell(r) for r in result["rows"] if team.lower() in str(r.get("team", "")).lower()]
    if mc:
        parts.append(f"{team}: " + ", ".join(mc))
    return ". ".join(parts)


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------

def _session_window(race: dict, sched_key: str, minutes: int) -> tuple[datetime, datetime] | None:
    sess = race if sched_key == "Race" else race.get(sched_key)
    if not sess or not sess.get("date"):
        return None
    t = (sess.get("time") or "00:00:00Z").rstrip("Z")
    try:
        start = datetime.fromisoformat(f"{sess['date']}T{t}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return start, start + timedelta(minutes=minutes)


async def collect(year: int, rnd: int, code: str, on_new=None, alert_until: float = 0) -> bool:
    """Collect one session if it isn't stored yet. Returns True when stored.

    ``on_new(year, rnd, code, result, fresh)`` runs once per new session; fresh
    is True while it's still worth alerting people (before ``alert_until``).
    """
    key = _key(year, rnd, code)
    if code in get_stored(year, rnd):
        return True
    if key in _inflight:  # a fast poll and a fallback attempt overlapping
        return False
    _inflight.add(key)
    try:
        try:
            result = await asyncio.to_thread(fetch_session, year, rnd, code)
        except Exception as e:
            logger.info("sessions: %s not ready yet (%s)", key, e)
            return False
        if not result:
            return False
        _save(year, rnd, code, result)
        _flag_seen.pop(key, None)
    finally:
        _inflight.discard(key)
    logger.info("sessions: collected %s (%d drivers)", key, len(result["rows"]))
    if on_new:
        try:
            await on_new(year, rnd, code, result, time.time() < alert_until)
        except Exception:
            logger.warning("sessions: on_new callback failed", exc_info=True)
    return True


async def plan_jobs(scheduler, on_new=None) -> int:
    """Schedule collection attempts after every session in the next 8 days, and
    catch up immediately on anything that ended recently but isn't stored."""
    from utils.mclaren import get_schedule
    from utils.f1_data import get_current_season

    year = get_current_season()
    now = datetime.now(timezone.utc)
    planned = 0
    stagger = 0
    for race in await get_schedule(year):
        rnd = int(race["round"])
        for sched_key, code, _name, minutes in SESSIONS:
            window = _session_window(race, sched_key, minutes)
            if not window:
                continue
            start, end = window
            if end < now - timedelta(days=3) or start > now + timedelta(days=8):
                continue
            alert_until = (end + _ALERT_WINDOW).timestamp()
            args = [year, rnd, code, on_new, alert_until]
            if code in _FAST_POLL:
                first, last = (start + timedelta(minutes=m) for m in _FAST_POLL[code])
                if last > now:
                    scheduler.add_job(
                        collect, "interval", minutes=_POLL_MINUTES,
                        start_date=max(first, now), end_date=last,
                        args=args, id=f"collect-{year}-{rnd}-{code}-poll",
                        replace_existing=True, coalesce=True, max_instances=1,
                        misfire_grace_time=60,
                    )
                    planned += 1
            if end <= now:
                if code not in get_stored(year, rnd):
                    stagger += 20  # missed while offline: catch up, spaced out
                    scheduler.add_job(
                        collect, "date", run_date=now + timedelta(seconds=stagger),
                        args=args, id=f"collect-{year}-{rnd}-{code}-now",
                        replace_existing=True, misfire_grace_time=3600,
                    )
                    planned += 1
                continue
            for off in _ATTEMPT_OFFSETS:
                scheduler.add_job(
                    collect, "date", run_date=end + timedelta(minutes=off),
                    args=args, id=f"collect-{year}-{rnd}-{code}-{off}",
                    replace_existing=True, misfire_grace_time=1800,
                )
                planned += 1
    logger.info("sessions: planned %d collection job(s)", planned)
    return planned


# --------------------------------------------------------------------------
# Driver display data (names, teams, official team colours)
# --------------------------------------------------------------------------

_META_KEY = "driver_meta_v1"


def _load_meta(year: int, rnd: int) -> dict:
    """Blocking: driver code -> surname, team, team colour from F1 timing."""
    for code in ("Q", "R", "FP3", "FP2", "FP1", "SQ", "S"):
        try:
            s = fastf1.get_session(year, rnd, code)
            s.load(laps=False, telemetry=False, weather=False, messages=False)
            res = s.results
            if res is None or len(res) == 0:
                continue
            out = {}
            for _, r in res.iterrows():
                abbr = str(r.get("Abbreviation") or "")
                if abbr:
                    color = str(r.get("TeamColor") or "")
                    out[abbr] = {
                        "name": str(r.get("LastName") or abbr),
                        "team": str(r.get("TeamName") or ""),
                        "color": f"#{color}" if len(color) == 6 else "",
                    }
            if out:
                return out
        except Exception as e:
            logger.info("driver meta: %s %s unavailable (%s)", rnd, code, e)
    return {}


async def driver_meta(year: int, rnd: int) -> dict:
    cache = store.load(_META_KEY, {}) or {}
    key = f"{year}-{rnd}"
    if cache.get(key):
        return cache[key]
    if year < 2018:  # F1 live timing (team colours) only goes back to 2018
        return {}
    meta = await asyncio.to_thread(_load_meta, year, rnd)
    if meta:
        cache[key] = meta
        for k in list(cache)[:-30]:  # keep the 30 most recent lookups
            cache.pop(k, None)
        store.save(_META_KEY, cache)
    return meta
