import datetime
import logging
import hashlib
import re
from collections import OrderedDict
import pandas as pd
import pytz
from telegram import Update
from telegram.ext import Application, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from utils.f1_data import get_event_schedule, IRISH_TZ, UTC_TZ
from utils.rate_limit import is_rate_limited
from utils.tavily_client import search
from utils.groq_client import chat, FAST_MODEL

logger = logging.getLogger(__name__)

_subscribers: set[int] = set()
_scheduler: AsyncIOScheduler | None = None
# Use OrderedDict as a bounded FIFO cache of seen-news hashes.
_MAX_SEEN_HASHES = 500
_seen_news_hashes: "OrderedDict[str, None]" = OrderedDict()

# Keywords that indicate breaking/important news. Matched as whole words.
BREAKING_KEYWORDS = [
    "announced", "confirmed", "signs", "signed", "joins", "sacked",
    "penalised", "penalized", "disqualified", "banned", "fined",
    "injured", "retires", "retirement",
    "debut", "reserve driver", "new driver", "new team",
    "rule change", "technical directive", "regulation change",
    "clinches", "clinched",
]
_BREAKING_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in BREAKING_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

NEWS_SOURCES = [
    "formula1.com",
    "autosport.com",
    "motorsport.com",
    "the-race.com",
    "gpfans.com",
    "planetf1.com",
    "f1i.com",
]


def setup_scheduler(application: Application) -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone=pytz.utc)
    try:
        _schedule_all_reminders(application)
    except Exception as e:
        logger.error(f"Failed to schedule session reminders: {e}")
    # Schedule news check every 30 minutes
    _scheduler.add_job(
        _check_breaking_news,
        trigger=IntervalTrigger(minutes=30),
        args=[application],
        id="news_check",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Session reminder scheduler started (news checks every 30min).")


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


def _hash_news(title: str, url: str) -> str:
    """Create a unique hash for a news item."""
    return hashlib.md5(f"{title}:{url}".encode()).hexdigest()


def _is_breaking_news(title: str, content: str) -> bool:
    """Check if news item contains breaking keywords (whole-word match)."""
    combined = f"{title} {content}"
    return bool(_BREAKING_RE.search(combined))


def _mark_seen(news_hash: str) -> None:
    """Record a hash in FIFO order, evicting the oldest when full."""
    if news_hash in _seen_news_hashes:
        _seen_news_hashes.move_to_end(news_hash)
        return
    _seen_news_hashes[news_hash] = None
    while len(_seen_news_hashes) > _MAX_SEEN_HASHES:
        _seen_news_hashes.popitem(last=False)


async def _check_breaking_news(application: Application) -> None:
    """Periodically check for breaking F1 news and push to subscribers."""
    if not _subscribers:
        return

    try:
        # Search for latest F1 news
        results = await search("F1 2026 latest news breaking announced", max_results=10)

        if not results:
            return

        breaking_items = []
        for item in results:
            title = item.get("title", "")
            url = item.get("url", "")
            content = item.get("content", "")

            # Check if this is breaking news
            if not _is_breaking_news(title, content):
                continue

            # Check if we've already seen this news
            news_hash = _hash_news(title, url)
            if news_hash in _seen_news_hashes:
                continue

            # Mark as seen (FIFO eviction).
            _mark_seen(news_hash)

            breaking_items.append({
                "title": title,
                "url": url,
                "content": content[:300],  # Truncate for summary
            })

        if not breaking_items:
            return

        # Summarize breaking news with LLM
        news_text = "\n\n".join([
            f"**{item['title']}**\n{item['content']}\nSource: {item['url']}"
            for item in breaking_items[:3]  # Limit to top 3
        ])

        prompt = f"""Summarize these breaking F1 news items in 2-3 sentences max.
Be concise and factual. Focus on the key announcement or development.

News:
{news_text}"""

        summary = await chat(messages=[{"role": "user", "content": prompt}], model=FAST_MODEL)

        # Send to all subscribers
        message = f"🚨 *Breaking F1 News*\n\n{summary}"
        for chat_id in list(_subscribers):
            try:
                await application.bot.send_message(
                    chat_id=chat_id, text=message, parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Failed to send news to {chat_id}: {e}")

        logger.info(f"Pushed {len(breaking_items)} breaking news items to {len(_subscribers)} subscribers")

    except Exception as e:
        logger.error(f"Error checking breaking news: {e}")


async def notify_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    chat_id = update.effective_chat.id
    if chat_id in _subscribers:
        _subscribers.discard(chat_id)
        await update.message.reply_text(
            "Reminders off. You won't get session alerts or breaking news anymore.\n"
            "Use /notify again to turn them back on."
        )
    else:
        _subscribers.add(chat_id)
        await update.message.reply_text(
            "Reminders on. You'll get:\n"
            "• 30-min alerts before each session\n"
            "• Breaking F1 news as it happens\n\n"
            "Use /notify again to turn them off."
        )
