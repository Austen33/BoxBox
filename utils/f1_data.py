import fastf1
import pandas as pd
import pytz
import aiohttp
from datetime import datetime
import os
import tempfile

cache_dir = os.path.join(tempfile.gettempdir(), "fastf1_cache")
os.makedirs(cache_dir, exist_ok=True)
fastf1.Cache.enable_cache(cache_dir)

IRISH_TZ = pytz.timezone("Europe/Dublin")
UTC_TZ = pytz.utc


def get_current_season() -> int:
    return datetime.now().year


def get_event_schedule(year: int = None) -> fastf1.events.EventSchedule:
    if year is None:
        year = get_current_season()
    return fastf1.get_event_schedule(year)


def get_next_event() -> dict | None:
    now_utc = datetime.now(UTC_TZ)
    schedule = get_event_schedule()

    for _, event in schedule.iterrows():
        if pd.isna(event.get("Session5Date")):
            continue
        race_date = event["Session5Date"]
        if hasattr(race_date, "tzinfo") and race_date.tzinfo is None:
            race_date = UTC_TZ.localize(race_date)
        elif not hasattr(race_date, "tzinfo"):
            continue
        if race_date > now_utc:
            return event.to_dict()

    return None


def format_session_time(dt, timezone=IRISH_TZ) -> str:
    if dt is None or (hasattr(dt, "__class__") and dt.__class__.__name__ == "NaTType"):
        return "TBC"
    if pd.isna(dt):
        return "TBC"
    if hasattr(dt, "tzinfo"):
        if dt.tzinfo is None:
            dt = UTC_TZ.localize(dt)
        local_dt = dt.astimezone(timezone)
    else:
        return "TBC"
    return local_dt.strftime("%a %d %b, %H:%M")


def get_next_race_info() -> dict | None:
    event = get_next_event()
    if event is None:
        return None

    sessions = {}
    session_names = {
        "Session1": event.get("Session1"),
        "Session2": event.get("Session2"),
        "Session3": event.get("Session3"),
        "Session4": event.get("Session4"),
        "Session5": event.get("Session5"),
    }
    session_dates = {
        "Session1": event.get("Session1Date"),
        "Session2": event.get("Session2Date"),
        "Session3": event.get("Session3Date"),
        "Session4": event.get("Session4Date"),
        "Session5": event.get("Session5Date"),
    }

    for key in ["Session1", "Session2", "Session3", "Session4", "Session5"]:
        name = session_names.get(key)
        date = session_dates.get(key)
        if name and not pd.isna(name):
            sessions[name] = format_session_time(date)

    race_date = event.get("Session5Date")
    now_utc = datetime.now(UTC_TZ)
    if race_date and not pd.isna(race_date):
        if hasattr(race_date, "tzinfo") and race_date.tzinfo is None:
            race_date = UTC_TZ.localize(race_date)
        delta = race_date - now_utc
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes = remainder // 60
        countdown = f"{days}d {hours}h {minutes}m"
    else:
        countdown = "TBC"

    return {
        "name": event.get("EventName", "Unknown Event"),
        "country": event.get("Country", ""),
        "location": event.get("Location", ""),
        "round": event.get("RoundNumber", ""),
        "sessions": sessions,
        "countdown": countdown,
        "race_date_formatted": format_session_time(event.get("Session5Date")),
    }


def get_last_race_results(year: int = None, round_number: int = None) -> dict | None:
    try:
        if year is None:
            year = get_current_season()

        schedule = get_event_schedule(year)
        now_utc = datetime.now(UTC_TZ)

        last_event = None
        for _, event in schedule.iterrows():
            race_date = event.get("Session5Date")
            if race_date is None or pd.isna(race_date):
                continue
            if hasattr(race_date, "tzinfo") and race_date.tzinfo is None:
                race_date = UTC_TZ.localize(race_date)
            if race_date < now_utc:
                last_event = event

        if last_event is None:
            return None

        session = fastf1.get_session(year, int(last_event["RoundNumber"]), "R")
        session.load(laps=True, telemetry=False, weather=False, messages=False)

        results = session.results
        if results is None or len(results) == 0:
            return None

        top_results = []
        for i, (_, driver) in enumerate(results.iterrows()):
            if i >= 10:
                break
            top_results.append({
                "position": driver.get("Position", i + 1),
                "driver": driver.get("FullName", driver.get("Abbreviation", "Unknown")),
                "team": driver.get("TeamName", "Unknown"),
                "abbreviation": driver.get("Abbreviation", ""),
            })

        return {
            "name": last_event.get("EventName", "Unknown"),
            "year": year,
            "round": int(last_event["RoundNumber"]),
            "results": top_results,
        }
    except Exception as e:
        return {"error": str(e)}


def get_qualifying_results(year: int = None, round_number: int = None) -> dict | None:
    try:
        if year is None:
            year = get_current_season()

        if round_number is None:
            schedule = get_event_schedule(year)
            now_utc = datetime.now(UTC_TZ)
            last_round = None
            for _, event in schedule.iterrows():
                qual_date = event.get("Session4Date")
                if qual_date is None or pd.isna(qual_date):
                    continue
                if hasattr(qual_date, "tzinfo") and qual_date.tzinfo is None:
                    qual_date = UTC_TZ.localize(qual_date)
                if qual_date < now_utc:
                    last_round = int(event["RoundNumber"])
            if last_round is None:
                return None
            round_number = last_round

        session = fastf1.get_session(year, round_number, "Q")
        session.load(laps=False, telemetry=False, weather=False, messages=False)

        results = session.results
        if results is None or len(results) == 0:
            return None

        qual_results = []
        for i, (_, driver) in enumerate(results.iterrows()):
            if i >= 20:
                break
            qual_results.append({
                "position": i + 1,
                "driver": driver.get("FullName", driver.get("Abbreviation", "Unknown")),
                "team": driver.get("TeamName", "Unknown"),
                "abbreviation": driver.get("Abbreviation", ""),
                "q1": str(driver.get("Q1", "")),
                "q2": str(driver.get("Q2", "")),
                "q3": str(driver.get("Q3", "")),
            })

        event = fastf1.get_event(year, round_number)
        return {
            "name": event.EventName,
            "year": year,
            "round": round_number,
            "results": qual_results,
        }
    except Exception as e:
        return {"error": str(e)}


def get_lap_data_for_strategy(year: int = None, round_number: int = None) -> dict | None:
    try:
        if year is None:
            year = get_current_season()

        if round_number is None:
            race_info = get_last_race_results(year)
            if race_info is None or "error" in race_info:
                return None
            round_number = race_info["round"]

        session = fastf1.get_session(year, round_number, "R")
        session.load(laps=True, telemetry=False, weather=False, messages=False)

        laps = session.laps
        results = session.results

        driver_strategies = {}
        for driver_abbr in laps["Driver"].unique():
            driver_laps = laps[laps["Driver"] == driver_abbr].copy()
            driver_laps = driver_laps.sort_values("LapNumber")

            stints = []
            current_compound = None
            stint_start = None

            for _, lap in driver_laps.iterrows():
                compound = lap.get("Compound", "UNKNOWN")
                lap_num = lap.get("LapNumber", 0)

                if compound != current_compound:
                    if current_compound is not None:
                        stints.append({
                            "compound": current_compound,
                            "start_lap": stint_start,
                            "end_lap": int(lap_num) - 1,
                            "laps": int(lap_num) - 1 - stint_start + 1,
                        })
                    current_compound = compound
                    stint_start = int(lap_num)

            if current_compound is not None and stint_start is not None:
                last_lap = int(driver_laps["LapNumber"].max())
                stints.append({
                    "compound": current_compound,
                    "start_lap": stint_start,
                    "end_lap": last_lap,
                    "laps": last_lap - stint_start + 1,
                })

            driver_row = results[results["Abbreviation"] == driver_abbr]
            full_name = driver_abbr
            team = "Unknown"
            position = None
            if len(driver_row) > 0:
                full_name = driver_row.iloc[0].get("FullName", driver_abbr)
                team = driver_row.iloc[0].get("TeamName", "Unknown")
                position = driver_row.iloc[0].get("Position")

            driver_strategies[driver_abbr] = {
                "name": full_name,
                "team": team,
                "position": position,
                "stints": stints,
            }

        total_laps = int(laps["LapNumber"].max()) if len(laps) > 0 else 0
        event = fastf1.get_event(year, round_number)

        return {
            "name": event.EventName,
            "year": year,
            "round": round_number,
            "total_laps": total_laps,
            "strategies": driver_strategies,
        }
    except Exception as e:
        return {"error": str(e)}


JOLPI_BASE = "https://api.jolpi.ca/ergast/f1"


async def resolve_round(year: int, circuit_name: str) -> int | None:
    """Resolve a circuit name to a round number for a given year via Ergast."""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{JOLPI_BASE}/{year}.json?limit=30"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            races = data["MRData"]["RaceTable"]["Races"]
            name_lower = circuit_name.lower().strip()

            for race in races:
                circuit = race.get("Circuit", {})
                circuit_id = circuit.get("circuitId", "").lower()
                circuit_name_api = circuit.get("circuitName", "").lower()
                race_name = race.get("raceName", "").lower()
                locality = circuit.get("Location", {}).get("locality", "").lower()
                country = circuit.get("Location", {}).get("country", "").lower()

                if (name_lower in circuit_id or circuit_id in name_lower or
                    name_lower in circuit_name_api or circuit_name_api in name_lower or
                    name_lower in race_name or race_name in name_lower or
                    name_lower in locality or name_lower in country):
                    return int(race["round"])

            return None
    except Exception:
        return None


def get_race_rewind_data(year: int, round_number: int) -> dict | None:
    """Load race data for a rewind summary: results, key events, track status."""
    try:
        session = fastf1.get_session(year, round_number, "R")
        session.load(laps=True, telemetry=False, weather=True, messages=True)

        results = session.results
        if results is None or len(results) == 0:
            return None

        # Top 10 finishers
        finishers = []
        for i, (_, driver) in enumerate(results.iterrows()):
            if i >= 10:
                break
            pos = driver.get("Position", i + 1)
            if pd.isna(pos):
                pos = i + 1
            finishers.append({
                "position": int(pos),
                "driver": driver.get("FullName", driver.get("Abbreviation", "Unknown")),
                "team": driver.get("TeamName", "Unknown"),
                "abbreviation": driver.get("Abbreviation", ""),
                "grid": int(driver.get("GridPosition", 0)) if not pd.isna(driver.get("GridPosition")) else 0,
                "status": str(driver.get("Status", "")),
                "points": float(driver.get("Points", 0)) if not pd.isna(driver.get("Points")) else 0,
            })

        # DNFs and notable statuses
        dnfs = []
        for _, driver in results.iterrows():
            status = str(driver.get("Status", ""))
            if status not in ("Finished", "") and not status.startswith("+"):
                dnfs.append({
                    "driver": driver.get("FullName", driver.get("Abbreviation", "Unknown")),
                    "team": driver.get("TeamName", "Unknown"),
                    "status": status,
                    "grid": int(driver.get("GridPosition", 0)) if not pd.isna(driver.get("GridPosition")) else 0,
                })

        # Track status events (SC, VSC, red flag)
        track_events = []
        if hasattr(session, 'track_status') and session.track_status is not None:
            for _, row in session.track_status.iterrows():
                status = str(row.get("Status", ""))
                message = str(row.get("Message", ""))
                if status != "1":  # 1 = all clear
                    track_events.append(f"Lap ~{row.get('LapNumber', '?')}: {message}")

        # Race control messages (penalties, investigations)
        rc_messages = []
        if hasattr(session, 'race_control_messages') and session.race_control_messages is not None:
            for _, msg in session.race_control_messages.head(20).iterrows():
                category = str(msg.get("Category", ""))
                message = str(msg.get("Message", ""))
                if category.lower() in ("penalty", "investigation", "flag", "safety car", "virtual safety car"):
                    rc_messages.append(f"{category}: {message}")

        # Pit stops summary (first driver only as example, top 5)
        pit_summary = []
        laps = session.laps
        if laps is not None and len(laps) > 0:
            for abbr in [f["abbreviation"] for f in finishers[:5] if f["abbreviation"]]:
                driver_laps = laps[laps["Driver"] == abbr]
                pit_laps = driver_laps[driver_laps["PitInTime"].notna()]
                if len(pit_laps) > 0:
                    pit_lap_nums = ", ".join(str(int(l)) for l in pit_laps["LapNumber"].tolist())
                    pit_summary.append(f"{abbr} pitted on laps: {pit_lap_nums}")

        # Fastest lap
        fastest_lap_info = ""
        if laps is not None and len(laps) > 0:
            valid_laps = laps[laps["LapTime"].notna()]
            if len(valid_laps) > 0:
                fl = valid_laps.loc[valid_laps["LapTime"].idxmin()]
                fastest_lap_info = f"Fastest lap: {fl['Driver']} 1:{fl['LapTime'].strftime('%M.%S.%f')[:8]} (Lap {int(fl['LapNumber'])})"

        # Weather
        weather_info = ""
        if hasattr(session, 'weather_data') and session.weather_data is not None and len(session.weather_data) > 0:
            w = session.weather_data.iloc[0]
            weather_info = f"Weather: {w.get('AirTemp', '?')}°C air, {w.get('TrackTemp', '?')}°C track, {w.get('Rainfall', 'No')} rain"

        event = fastf1.get_event(year, round_number)

        return {
            "name": event.EventName,
            "year": year,
            "round": round_number,
            "total_laps": int(laps["LapNumber"].max()) if laps is not None and len(laps) > 0 else 0,
            "finishers": finishers,
            "dnfs": dnfs,
            "track_events": track_events,
            "rc_messages": rc_messages,
            "pit_summary": pit_summary,
            "fastest_lap": fastest_lap_info,
            "weather": weather_info,
        }
    except Exception as e:
        return {"error": str(e)}


async def get_driver_standings(year: int = None) -> dict | None:
    if year is None:
        year = get_current_season()
    url = f"{JOLPI_BASE}/{year}/driverStandings.json?limit=10"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {"error": f"API returned {resp.status}"}
                data = await resp.json()

        lists = data["MRData"]["StandingsTable"]["StandingsLists"]
        if not lists:
            return {"error": "No standings data available yet"}

        standings_list = lists[0]
        drivers = []
        for entry in standings_list["DriverStandings"]:
            drivers.append({
                "position": int(entry["position"]),
                "driver": f"{entry['Driver']['givenName']} {entry['Driver']['familyName']}",
                "code": entry["Driver"].get("code", ""),
                "team": entry["Constructors"][0]["name"] if entry["Constructors"] else "Unknown",
                "points": float(entry["points"]),
                "wins": int(entry["wins"]),
            })

        return {
            "year": year,
            "round": int(standings_list["round"]),
            "drivers": drivers,
        }
    except Exception as e:
        return {"error": str(e)}


async def get_constructor_standings(year: int = None) -> dict | None:
    if year is None:
        year = get_current_season()
    url = f"{JOLPI_BASE}/{year}/constructorStandings.json?limit=10"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {"error": f"API returned {resp.status}"}
                data = await resp.json()

        lists = data["MRData"]["StandingsTable"]["StandingsLists"]
        if not lists:
            return {"error": "No standings data available yet"}

        standings_list = lists[0]
        constructors = []
        for entry in standings_list["ConstructorStandings"]:
            constructors.append({
                "position": int(entry["position"]),
                "team": entry["Constructor"]["name"],
                "points": float(entry["points"]),
                "wins": int(entry["wins"]),
            })

        return {
            "year": year,
            "round": int(standings_list["round"]),
            "constructors": constructors,
        }
    except Exception as e:
        return {"error": str(e)}
