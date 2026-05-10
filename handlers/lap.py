import asyncio
from datetime import datetime
import fastf1
import pandas as pd
from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import get_current_season, get_event_schedule, UTC_TZ
from utils.groq_client import chat, FAST_MODEL
from utils.rate_limit import is_rate_limited

VALID_SESSIONS = {"FP1", "FP2", "FP3", "Q", "S", "SQ", "R"}


def _last_completed_round(year: int, session_type: str) -> tuple[int, str] | None:
    schedule = get_event_schedule(year)
    now_utc = datetime.now(UTC_TZ)

    date_key = "Session5Date" if session_type == "R" else "Session4Date"

    last = None
    for _, event in schedule.iterrows():
        d = event.get(date_key)
        if d is None or pd.isna(d):
            continue
        if hasattr(d, "tzinfo") and d.tzinfo is None:
            d = UTC_TZ.localize(d)
        if d < now_utc:
            last = (int(event["RoundNumber"]), event.get("EventName", "Unknown"))
    return last


def _fetch_fastest_lap(year: int, round_num: int, session_type: str, driver: str) -> dict | None:
    try:
        session = fastf1.get_session(year, round_num, session_type)
        session.load(laps=True, telemetry=False, weather=False, messages=False)
        driver_laps = session.laps.pick_driver(driver)
        if driver_laps is None or len(driver_laps) == 0:
            return None
        fastest = driver_laps.pick_fastest()
        if fastest is None:
            return None
        all_laps = session.laps
        best_s1 = all_laps["Sector1Time"].min()
        best_s2 = all_laps["Sector2Time"].min()
        best_s3 = all_laps["Sector3Time"].min()

        return {
            "lap_time": str(fastest.get("LapTime", "")),
            "compound": str(fastest.get("Compound", "?")),
            "lap_num": int(fastest.get("LapNumber", 0) or 0),
            "s1": str(fastest.get("Sector1Time", "")),
            "s2": str(fastest.get("Sector2Time", "")),
            "s3": str(fastest.get("Sector3Time", "")),
            "best_s1": str(best_s1) if pd.notna(best_s1) else "N/A",
            "best_s2": str(best_s2) if pd.notna(best_s2) else "N/A",
            "best_s3": str(best_s3) if pd.notna(best_s3) else "N/A",
        }
    except Exception as e:
        return {"error": str(e)}


async def lap_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /lap [driver] [session]\n"
            "Example: /lap VER Q\n"
            "Sessions: FP1, FP2, FP3, Q, S, SQ, R (default R)"
        )
        return

    driver = args[0].upper()
    session_type = args[1].upper() if len(args) > 1 else "R"
    if session_type not in VALID_SESSIONS:
        await update.message.reply_text(
            f"Unknown session '{session_type}'. Try one of: {', '.join(sorted(VALID_SESSIONS))}."
        )
        return

    await update.message.reply_chat_action("typing")

    year = get_current_season()
    last = await asyncio.to_thread(_last_completed_round, year, session_type)
    if last is None:
        await update.message.reply_text("No completed sessions found this season yet.")
        return

    round_num, event_name = last
    data = await asyncio.to_thread(_fetch_fastest_lap, year, round_num, session_type, driver)

    if data is None:
        await update.message.reply_text(
            f"No lap data for {driver} in {session_type} at {event_name}. "
            f"Check the abbreviation (e.g. VER, NOR, LEC)."
        )
        return
    if "error" in data:
        await update.message.reply_text(
            f"Couldn't load {session_type} for {event_name}: {data['error']}"
        )
        return

    data_text = (
        f"*{driver}* — {session_type} at {event_name} {year}\n"
        f"Fastest: {data['lap_time']} (Lap {data['lap_num']}, {data['compound']})\n"
        f"S1 {data['s1']} | S2 {data['s2']} | S3 {data['s3']}\n"
        f"Session best — S1 {data['best_s1']} | S2 {data['best_s2']} | S3 {data['best_s3']}"
    )

    prompt = f"""{data_text}

Give a brief 2-3 sentence engineer-style read on this lap: which sectors were
personal strengths vs where time was lost to the session best, the tyre choice,
and how it stacks up. Use only the data above. No filler."""

    response = await chat(messages=[{"role": "user", "content": prompt}], model=FAST_MODEL)
    await update.message.reply_text(f"{data_text}\n\n{response}", parse_mode="Markdown")
