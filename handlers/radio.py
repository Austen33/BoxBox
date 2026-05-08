import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
import fastf1
import pandas as pd

from utils.f1_data import get_current_season, get_event_schedule, UTC_TZ
from utils.groq_client import chat, FAST_MODEL
from utils.rate_limit import is_rate_limited


def _get_last_completed_session(year: int) -> tuple[int, str, str] | None:
    """Returns (round_number, event_name, session_type) for last completed session."""
    schedule = get_event_schedule(year)
    now_utc = datetime.now(UTC_TZ)

    # Check sessions in priority order: R, Q, S, FP3, FP2, FP1
    session_map = [
        ("Session5Date", "Session5", "R"),
        ("Session4Date", "Session4", "Q"),
        ("Session3Date", "Session3", "S"),
        ("Session2Date", "Session2", "FP3"),
        ("Session1Date", "Session1", "FP1"),
    ]

    for _, event in schedule.iterrows():
        for date_key, name_key, sess_type in session_map:
            d = event.get(date_key)
            if d is None or pd.isna(d):
                continue
            if hasattr(d, "tzinfo") and d.tzinfo is None:
                d = UTC_TZ.localize(d)
            if d < now_utc:
                return (int(event["RoundNumber"]), event.get("EventName", "Unknown"), sess_type)

    return None


def _fetch_team_radio(year: int, round_num: int, session_type: str, driver: str) -> list[dict]:
    """Fetch team radio messages for a driver in a session."""
    try:
        session = fastf1.get_session(year, round_num, session_type)
        session.load(laps=True, telemetry=False, weather=False, messages=True)

        # Get team radio data
        radio_data = session.race_control_messages if hasattr(session, 'race_control_messages') else None

        # Try to get car data which includes radio
        radio_messages = []

        # FastF1 stores radio in different places depending on version
        if hasattr(session, 'car_data'):
            for drv in [driver]:
                try:
                    car_data = session.car_data[drv]
                    if hasattr(car_data, 'radio') and car_data.radio is not None:
                        for msg in car_data.radio:
                            radio_messages.append({
                                "time": str(msg.get('Time', '')),
                                "message": msg.get('Message', ''),
                            })
                except (KeyError, AttributeError):
                    pass

        # Alternative: check session results for radio transcripts
        if not radio_messages and hasattr(session, 'results'):
            # Some sessions have radio transcripts embedded
            pass

        return radio_messages[:10]  # Limit to 10 messages
    except Exception as e:
        return [{"error": str(e)}]


def _fetch_race_control_messages(year: int, round_num: int, session_type: str) -> list[dict]:
    """Fetch race control messages as a fallback."""
    try:
        session = fastf1.get_session(year, round_num, session_type)
        session.load(laps=False, telemetry=False, weather=False, messages=True)

        messages = []
        if hasattr(session, 'race_control_messages'):
            for _, msg in session.race_control_messages.iterrows():
                messages.append({
                    "time": str(msg.get('Time', '')),
                    "category": msg.get('Category', ''),
                    "message": msg.get('Message', ''),
                })

        return messages[:15]
    except Exception as e:
        return [{"error": str(e)}]


async def radio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /radio command for team radio transcripts."""
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    args = context.args or []
    driver = args[0].upper() if args else None

    if not driver:
        await update.message.reply_text(
            "Usage: /radio [driver]\n"
            "Example: /radio VER\n"
            "Returns team radio from the most recent session."
        )
        return

    await update.message.reply_chat_action("typing")

    year = get_current_season()
    last_session = await asyncio.to_thread(_get_last_completed_session, year)

    if last_session is None:
        await update.message.reply_text("No completed sessions found this season yet.")
        return

    round_num, event_name, sess_type = last_session
    radio_messages = await asyncio.to_thread(_fetch_team_radio, year, round_num, sess_type, driver)

    if not radio_messages:
        # Fallback to race control messages if no team radio
        rc_messages = await asyncio.to_thread(_fetch_race_control_messages, year, round_num, sess_type)

        if rc_messages and "error" not in rc_messages[0]:
            rc_text = f"*Race Control Messages* — {sess_type} at {event_name}\n\n"
            for msg in rc_messages[:8]:
                rc_text += f"_{msg['time']}_: {msg['message']}\n"

            prompt = f"""{rc_text}

Summarize the key incidents and decisions from these race control messages in 2-3 sentences.
Focus on flags, penalties, and notable events."""
            response = await chat(messages=[{"role": "user", "content": prompt}], model=FAST_MODEL)
            await update.message.reply_text(f"{rc_text}\n{response}", parse_mode="Markdown")
            return

        await update.message.reply_text(
            f"No team radio available for {driver} in {sess_type} at {event_name}.\n"
            f"Team radio is typically only available for race sessions (R)."
        )
        return

    if "error" in radio_messages[0]:
        await update.message.reply_text(f"Couldn't fetch radio data: {radio_messages[0]['error']}")
        return

    radio_text = f"*{driver} Team Radio* — {sess_type} at {event_name}\n\n"
    for msg in radio_messages:
        radio_text += f"_{msg['time']}_: {msg['message']}\n"

    prompt = f"""{radio_text}

Summarize the key messages from this team radio in 2-3 sentences.
What was the main topic or concern?"""

    response = await chat(messages=[{"role": "user", "content": prompt}], model=FAST_MODEL)
    await update.message.reply_text(f"{radio_text}\n{response}", parse_mode="Markdown")
