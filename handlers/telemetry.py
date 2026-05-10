import asyncio
import logging
import traceback
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
import fastf1
import pandas as pd
import numpy as np

from utils.f1_data import get_current_season, get_event_schedule, UTC_TZ, cache_dir
from utils.groq_client import chat, SMART_MODEL
from utils.rate_limit import is_rate_limited
from fastf1._api import car_data as api_car_data
from fastf1.core import Telemetry

logger = logging.getLogger(__name__)


def _get_last_race_session(year: int) -> tuple[int, str] | None:
    """Returns (round_number, event_name) for last completed race."""
    schedule = get_event_schedule(year)
    now_utc = datetime.now(UTC_TZ)

    last_match = None
    for _, event in schedule.iterrows():
        d = event.get("Session5Date")
        if d is None or pd.isna(d):
            continue
        if hasattr(d, "tzinfo") and d.tzinfo is None:
            d = UTC_TZ.localize(d)
        if d < now_utc:
            last_match = (int(event["RoundNumber"]), event.get("EventName", "Unknown"))

    return last_match


def _load_car_data_manual(session) -> dict:
    """Manually fetch and process car telemetry data, bypassing session.load(telemetry=True)."""
    try:
        raw = api_car_data(session.api_path)
    except Exception as e:
        logger.warning(f"api_car_data failed: {e}")
        return {}

    if not raw:
        return {}

    session._calculate_t0_date(raw, {})

    car_data = {}
    for drv in session.drivers:
        if drv not in raw:
            continue
        try:
            drv_car = Telemetry(
                raw[drv].drop(labels='Time', axis=1),
                session=session,
                driver=drv,
                drop_unknown_channels=True,
                _cast_default_cols=True
            )
        except Exception:
            continue

        drv_car['Date'] = drv_car['Date'].dt.round('ms')
        drv_car['Time'] = drv_car['Date'] - session.t0_date
        drv_car['SessionTime'] = drv_car['Time']

        car_data[drv] = drv_car

    return car_data


def _get_car_data_for_lap(session, lap, car_data_dict: dict):
    """Get car data for a specific lap from a pre-loaded car_data dict."""
    drv_num = lap['DriverNumber']
    if drv_num not in car_data_dict:
        raise ValueError(f"No car data for driver {drv_num}")
    return car_data_dict[drv_num].slice_by_lap(lap).reset_index(drop=True)


def _fetch_telemetry_comparison(year: int, round_num: int, driver1: str, driver2: str, corner: int | None = None) -> dict:
    """Fetch and compare telemetry between two drivers."""
    try:
        session = fastf1.get_session(year, round_num, "R")
        session.load(laps=True, telemetry=False, weather=False, messages=False)
        session._load_telemetry()

        results = {}

        for driver in [driver1, driver2]:
            driver_laps = session.laps.pick_driver(driver)
            if driver_laps is None or len(driver_laps) == 0:
                return {"error": f"No data for {driver}"}

            fastest = driver_laps.pick_fastest()
            if fastest is None:
                return {"error": f"No fastest lap for {driver}"}

            try:
                car_data = fastest.get_car_data()
            except Exception as car_err:
                logger.warning(f"get_car_data failed for {driver}: {car_err}")
                return {"error": f"Telemetry unavailable for {driver}: {car_err}"}
            if car_data is None or len(car_data) == 0:
                return {"error": f"No telemetry for {driver}"}

            # Get sector times
            s1 = fastest.get('Sector1Time', None)
            s2 = fastest.get('Sector2Time', None)
            s3 = fastest.get('Sector3Time', None)

            # Calculate speed stats
            speeds = car_data['Speed']
            throttle = car_data['Throttle']
            brake = car_data['Brake']

            # Find corner data if specified
            corner_data = None
            if corner is not None:
                # Approximate corner by distance percentage
                total_distance = fastest.get('Distance', 0) or car_data['Distance'].max()
                corner_start = (corner - 1) * 0.1 * total_distance  # Rough corner position
                corner_end = corner * 0.1 * total_distance

                corner_mask = (car_data['Distance'] >= corner_start) & (car_data['Distance'] <= corner_end)
                if corner_mask.any():
                    corner_data = {
                        "avg_speed": float(car_data.loc[corner_mask, 'Speed'].mean()),
                        "max_speed": float(car_data.loc[corner_mask, 'Speed'].max()),
                        "avg_throttle": float(car_data.loc[corner_mask, 'Throttle'].mean()),
                        "brake_pct": float((car_data.loc[corner_mask, 'Brake'] > 0).mean() * 100),
                    }

            results[driver] = {
                "lap_time": str(fastest.get('LapTime', '')),
                "lap_number": int(fastest.get('LapNumber', 0) or 0),
                "tyre": str(fastest.get('Compound', '?')),
                "sector1": str(s1) if s1 else "N/A",
                "sector2": str(s2) if s2 else "N/A",
                "sector3": str(s3) if s3 else "N/A",
                "top_speed": float(speeds.max()),
                "avg_speed": float(speeds.mean()),
                "full_throttle_pct": float((throttle > 95).mean() * 100),
                "brake_zones": int((brake.diff().fillna(0) > 0.5).sum()),
                "corner_data": corner_data,
            }

        # Calculate deltas
        if driver1 in results and driver2 in results:
            d1 = results[driver1]
            d2 = results[driver2]

            # Parse lap times for delta calculation
            def parse_lap_time(lt):
                if not lt or lt == "N/A":
                    return None
                parts = str(lt).split(':')
                if len(parts) == 2:
                    return float(parts[0]) * 60 + float(parts[1])
                return float(lt)

            t1 = parse_lap_time(d1['lap_time'])
            t2 = parse_lap_time(d2['lap_time'])

            results["delta"] = {
                "lap_time": f"{(t1 - t2):.3f}s" if t1 and t2 else "N/A",
                "top_speed": f"{d1['top_speed'] - d2['top_speed']:.1f} km/h",
                "faster_driver": driver1 if t1 and t2 and t1 < t2 else driver2 if t1 and t2 else "N/A",
            }

        results["event"] = session.event.EventName
        results["year"] = year
        return results

    except Exception as e:
        logger.error(f"_fetch_telemetry_comparison failed: {traceback.format_exc()}")
        return {"error": str(e)}


async def telemetry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /telemetry command for driver comparison."""
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /telemetry [driver1] [driver2] [corner]\n"
            "Example: /telemetry VER NOR 1\n"
            "Compares fastest lap telemetry from the last race.\n"
            "Corner is optional (1-10 approximates track sectors)."
        )
        return

    driver1 = args[0].upper()
    driver2 = args[1].upper()
    corner = int(args[2]) if len(args) > 2 else None

    await update.message.reply_chat_action("typing")

    year = get_current_season()
    last_race = await asyncio.to_thread(_get_last_race_session, year)

    if last_race is None:
        await update.message.reply_text("No completed races found this season yet.")
        return

    round_num, event_name = last_race
    data = await asyncio.to_thread(_fetch_telemetry_comparison, year, round_num, driver1, driver2, corner)

    if "error" in data:
        await update.message.reply_text(f"Couldn't fetch telemetry: {data['error']}")
        return

    d1 = data.get(driver1, {})
    d2 = data.get(driver2, {})
    delta = data.get("delta", {})

    text = f"*Telemetry Comparison* — {event_name} {year}\n\n"
    text += f"*{driver1}* vs *{driver2}*\n\n"

    text += f"Driver | Lap Time | S1 | S2 | S3 | Tyre\n"
    text += f"---|---|---|---|---|---\n"
    text += f"{driver1} | {d1.get('lap_time', '?')} | {d1.get('sector1', '?')} | {d1.get('sector2', '?')} | {d1.get('sector3', '?')} | {d1.get('tyre', '?')}\n"
    text += f"{driver2} | {d2.get('lap_time', '?')} | {d2.get('sector1', '?')} | {d2.get('sector2', '?')} | {d2.get('sector3', '?')} | {d2.get('tyre', '?')}\n\n"

    text += f"Driver | Top Speed | Avg Speed | Full Throttle % | Brake Zones\n"
    text += f"---|---|---|---|---\n"
    text += f"{driver1} | {d1.get('top_speed', 0):.1f} km/h | {d1.get('avg_speed', 0):.1f} km/h | {d1.get('full_throttle_pct', 0):.0f}% | {d1.get('brake_zones', 0)}\n"
    text += f"{driver2} | {d2.get('top_speed', 0):.1f} km/h | {d2.get('avg_speed', 0):.1f} km/h | {d2.get('full_throttle_pct', 0):.0f}% | {d2.get('brake_zones', 0)}\n\n"

    if delta:
        faster = delta.get('faster_driver', '?')
        text += f"*Delta:* {delta.get('lap_time', '?')} — {faster} faster\n"
        text += f"Top speed difference: {delta.get('top_speed', '?')}\n"

    # Corner analysis if provided
    corner_analysis = ""
    if corner and d1.get('corner_data') and d2.get('corner_data'):
        c1 = d1['corner_data']
        c2 = d2['corner_data']
        corner_analysis = f"\n*Corner {corner} Analysis:*\n"
        corner_analysis += f"{driver1}: {c1['avg_speed']:.1f} km/h avg, {c1['brake_pct']:.0f}% braking\n"
        corner_analysis += f"{driver2}: {c2['avg_speed']:.1f} km/h avg, {c2['brake_pct']:.0f}% braking\n"

    prompt = f"""{text}{corner_analysis}

Analyze this telemetry comparison in 3-4 sentences. Focus on:
1. Where is the time difference coming from (sectors, speed, braking)?
2. What driving style differences does the data suggest?
3. Any setup or tyre differences that might explain the gap?

Be technical but accessible, like a race engineer explaining to a knowledgeable fan."""

    response = await chat(messages=[{"role": "user", "content": prompt}], model=SMART_MODEL)
    await update.message.reply_text(f"{text}{corner_analysis}\n\n{response}", parse_mode="Markdown")
