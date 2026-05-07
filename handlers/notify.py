import datetime
import logging
import pandas as pd
import pytz
from telegram import Update
from telegram.ext import Application, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from utils.f1_data import get_event_schedule, IRISH_TZ, UTC_TZ
from utils.rate_limit import is_rate_limited

logger = logging.getLogger(__name__)

_subscribers: set[int] = set()
_scheduler: AsyncIOScheduler | None = None


def setup_scheduler(application: Application) -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone=pytz.utc)
    try:
        _schedule_all_reminders(application)
    except Exception as e:
        logger.error(f"Failed to schedule session reminders: {e}")
    _scheduler.start()
    logger.info("Session reminder scheduler started.")


def _schedule_all_reminders(application: Application) -> None:
    schedule = get_event_schedule()
    now = datetime.datetime.now(UTC_TZ)

    session_keys = ["Session1Date", "Session2Date", "Session3Date", "Session4Date", "Session5Date"]
    name_keys = ["Session1", "Session2", "Session3", "Session4", "Session5"]

    scheduled = 0
    for _, event in schedule.iterrows():
        for date_key, name_key in zip(session_keys, name_keys):
            raw = event.get(date_key)
            if raw is None or pd.isna(raw):
                continue
            try:
                if hasattr(raw, "tzinfo") and raw.tzinfo is None:
                    raw = UTC_TZ.localize(raw)
                reminder_time = raw - datetime.timedelta(minutes=30)
                if reminder_time <= now:
                    continue
                label = event.get(name_key) or name_key
                local_time = raw.astimezone(IRISH_TZ).strftime("%H:%M Irish time")
                msg = (
                    f"⏱ *{label}* for *{event.get('EventName', 'next race')}* "
                    f"starts in 30 minutes ({local_time})."
                )
                _scheduler.add_job(
                    _send_reminder,
                    trigger=DateTrigger(run_date=reminder_time),
                    args=[application, msg],
                    misfire_grace_time=120,
                )
                scheduled += 1
            except Exception as e:
                logger.warning(
                    f"Could not schedule {name_key} for {event.get('EventName', '?')}: {e}"
                )

    logger.info(f"Scheduled {scheduled} session reminders.")


async def _send_reminder(application: Application, message: str) -> None:
    for chat_id in list(_subscribers):
        try:
            await application.bot.send_message(
                chat_id=chat_id, text=message, parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to send reminder to {chat_id}: {e}")


async def notify_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    chat_id = update.effective_chat.id
    if chat_id in _subscribers:
        _subscribers.discard(chat_id)
        await update.message.reply_text(
            "Reminders off. You won't get session alerts anymore.\n"
            "Use /notify again to turn them back on."
        )
    else:
        _subscribers.add(chat_id)
        await update.message.reply_text(
            "Reminders on. You'll get a message 30 minutes before each session.\n"
            "Use /notify again to turn them off."
        )
