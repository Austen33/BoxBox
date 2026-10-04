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
from utils.f1_data import get_event_schedule, get_current_season, IRISH_TZ, UTC_TZ
from utils.rate_limit import is_rate_limited
from utils.tavily_client import search
from utils.groq_client import chat, FAST_MODEL
from utils.telegram_safe import safe_send
from utils import store, mclaren
from handlers.follow import match_follows

logger = logging.getLogger(__name__)

_subscribers: set[int] = set()
_scheduler: AsyncIOScheduler | None = None
# Use OrderedDict as a bounded FIFO cache of seen-news hashes.
_MAX_SEEN_HASHES = 500
_seen_news_hashes: "OrderedDict[str, None]" = OrderedDict()

# Persistence keys for utils.store (survives restarts).
_SUBSCRIBERS_KEY = "notify_subscribers"
_SEEN_NEWS_KEY = "notify_seen_news"


def _load_state() -> None:
    """Rehydrate subscribers and seen-news hashes from the persistent store."""
    global _subscribers, _seen_news_hashes
    subs = store.load(_SUBSCRIBERS_KEY, [])
    if isinstance(subs, list):
        _subscribers = {int(s) for s in subs}
    seen = store.load(_SEEN_NEWS_KEY, [])
    if isinstance(seen, list):
        _seen_news_hashes = OrderedDict((h, None) for h in seen[-_MAX_SEEN_HASHES:])
    logger.info(
        "Loaded %d subscriber(s) and %d seen-news hash(es) from store.",
        len(_subscribers), len(_seen_news_hashes),
    )


def _persist_subscribers() -> None:
    store.save(_SUBSCRIBERS_KEY, sorted(_subscribers))


def _persist_seen_news() -> None:
    store.save(_SEEN_NEWS_KEY, list(_seen_news_hashes.keys()))

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

def setup_scheduler(application: Application) -> None:
    global _scheduler
    _load_state()
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
    # McLaren session alerts (qualifying and race results) every 10 minutes.
    _scheduler.add_job(
        _check_mclaren_sessions,
        trigger=IntervalTrigger(minutes=10),
        args=[application],
        id="mclaren_sessions",
        replace_existing=True,
    )
    # Auto-collect every session from F1 live timing right after it ends:
    # plan now, then re-plan every 6h so new weekends get picked up.
    _scheduler.add_job(
        _plan_session_collection,
        trigger=IntervalTrigger(hours=6),
        args=[application],
        id="plan_sessions",
        replace_existing=True,
        next_run_time=datetime.datetime.now(pytz.utc) + datetime.timedelta(seconds=15),
    )
    _scheduler.start()
    logger.info("Scheduler started (news checks every 30min, McLaren result alerts every 10min, live results after each session).")


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
        await safe_send(application.bot, chat_id, message)


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
    _persist_seen_news()


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

        # Send to all subscribers, flagging items that mention a followed
        # driver/team so /follow users see why it's relevant to them.
        base_message = f"🚨 *Breaking F1 News*\n\n{summary}"
        combined_text = " ".join(f"{i['title']} {i['content']}" for i in breaking_items)
        for chat_id in list(_subscribers):
            message = base_message
            hits = match_follows(chat_id, combined_text)
            if hits:
                message = f"⭐ Mentions {', '.join(hits)} you follow\n\n{base_message}"
            await safe_send(application.bot, chat_id, message)

        logger.info(f"Pushed {len(breaking_items)} breaking news items to {len(_subscribers)} subscribers")

    except Exception as e:
        logger.error(f"Error checking breaking news: {e}")


# Sessions pushed to subscribers as soon as live timing has them.
_ALERT_SESSIONS = {"Q": ("⏱", "qualifying"), "S": ("🏁", "sprint result"), "R": ("🏁", "race result")}
_PUSHED_KEY = "session_alerts_pushed"


def _pushed() -> list[str]:
    return store.load(_PUSHED_KEY, []) or []


def _mark_pushed(key: str) -> None:
    keys = [k for k in _pushed() if k != key][-59:]
    store.save(_PUSHED_KEY, keys + [key])


async def _session_alert_text(year: int, rnd: int, code: str, result: dict) -> str:
    from utils import sessions
    icon, what = _ALERT_SESSIONS[code]
    rows = result["rows"]

    def line(r):
        extra = r.get("time") or r.get("note") or ""
        return f"P{r['pos']} {r['driver']} ({r['team']})" + (f", {extra}" if extra else "")

    table = "\n".join(line(r) for r in rows[:10])
    mcl = [f"P{r['pos']} {r['driver']}" for r in rows if "mclaren" in str(r.get("team", "")).lower()]
    text = f"{icon} *{result['event']}: {what}*\n_Provisional, from live timing_\n\n{table}"
    if mcl:
        text += f"\n\nMcLaren: {', '.join(mcl)}"

    facts = "\n".join(line(r) for r in rows)
    if code == "R":
        quali = sessions.get_stored(year, rnd).get("Q")
        if quali:
            facts += "\n\nQualifying order (before any grid penalties): " + ", ".join(
                f"P{r['pos']} {r['driver']}" for r in quali["rows"])
    prompt = f"""{result['event']} {result['session']} classification (provisional, from live timing):
{facts}

Write 2 to 3 short sentences for a Telegram chat: who won (or took pole) and the McLaren angle.
Use only the facts above. Don't invent gaps, pit stops, incidents or reasons for retirements."""
    try:
        summary = await chat(messages=[{"role": "user", "content": prompt}], model=FAST_MODEL)
        if summary:
            text += f"\n\n{summary.strip()}"
    except Exception:
        logger.warning("session alert summary failed", exc_info=True)
    return text


async def _on_session_collected(application: Application, year: int, rnd: int, code: str,
                                result: dict, fresh: bool) -> None:
    """A session just finished: refresh news, and push results to subscribers."""
    from utils import news
    news.invalidate()

    key = f"{year}-{rnd}-{code}"
    if not fresh or code not in _ALERT_SESSIONS or not _subscribers or key in _pushed():
        return
    text = await _session_alert_text(year, rnd, code, result)
    # Persist before sending so a send failure can't cause a repeat.
    _mark_pushed(key)
    for chat_id in list(_subscribers):
        hits = match_follows(chat_id, " ".join(r["driver"] for r in result["rows"][:10]))
        prefix = f"⭐ {', '.join(hits)} you follow\n\n" if hits else ""
        await safe_send(application.bot, chat_id, prefix + text)
    logger.info("Pushed %s results to %d subscriber(s)", key, len(_subscribers))


async def _plan_session_collection(application: Application) -> None:
    from utils import sessions

    async def on_new(*args):
        await _on_session_collected(application, *args)

    try:
        await sessions.plan_jobs(_scheduler, on_new=on_new)
    except Exception:
        logger.exception("Planning session collection failed")


_MCLAREN_STATE_KEY = "mclaren_alert_state"


async def _check_mclaren_sessions(application: Application) -> None:
    """Push a McLaren-angled alert when new qualifying or race results appear."""
    try:
        markers = await mclaren.latest_session_markers()
        if not any(markers.values()):
            return
        state = store.load(_MCLAREN_STATE_KEY, None)
        if not isinstance(state, dict):
            # First run: record where we are so we don't replay old sessions.
            store.save(_MCLAREN_STATE_KEY, markers)
            return

        alerts = []
        year = get_current_season()
        pushed = _pushed()
        race_key, quali_key = f"{year}-{markers['race']}-R", f"{year}-{markers['quali']}-Q"
        race_new = markers["race"] > state.get("race", 0)
        quali_new = markers["quali"] > state.get("quali", 0)
        # Race first (a new race round also implies its quali is old news).
        # Sessions already pushed from live timing just get their marker updated.
        if race_new and race_key not in pushed:
            name, data = await mclaren.debrief_text()
            if data:
                prompt = f"""{data}

Write a McLaren post-race alert for a Telegram chat: 3 to 4 short sentences. Lead with how Norris and Piastri
finished and the points scored, then the championship impact. Be honest if it was a bad day.
Use only the facts above."""
                alerts.append(f"🏁 *{name}: McLaren result*\n\n" + await chat(
                    messages=[{"role": "user", "content": prompt}], model=FAST_MODEL))
        elif not race_new and quali_new and quali_key not in pushed:
            name, data = await mclaren.quali_alert_text()
            if data:
                prompt = f"""{data}

Write a McLaren qualifying alert for a Telegram chat in 2 to 3 short sentences: where Norris and Piastri
start and how far off pole they are. Be honest if it went badly. Use only the facts above."""
                alerts.append(f"⏱ *{name}: qualifying*\n\n" + await chat(
                    messages=[{"role": "user", "content": prompt}], model=FAST_MODEL))

        # Persist before sending so a send failure can't cause a repeat storm.
        store.save(_MCLAREN_STATE_KEY, markers)
        if alerts:
            _mark_pushed(race_key if race_new else quali_key)
        for text in alerts:
            for chat_id in list(_subscribers):
                await safe_send(application.bot, chat_id, text)
        if alerts:
            logger.info("Sent %d McLaren alert(s) to %d subscriber(s)", len(alerts), len(_subscribers))
    except Exception:
        logger.exception("McLaren session check failed")


async def subscribe_core(message, chat_id: int) -> None:
    """Add-only subscribe used by the race-weekend hub button. Idempotent."""
    if chat_id in _subscribers:
        await message.reply_text(
            "Reminders are already on. Use /notify to turn them off."
        )
        return
    _subscribers.add(chat_id)
    _persist_subscribers()
    await message.reply_text(
        "Reminders on. You'll get:\n"
        "• 30-min alerts before each session\n"
        "• Qualifying, sprint and race results minutes after the flag\n"
        "• Breaking F1 news as it happens\n\n"
        "Use /notify again to turn them off."
    )


async def notify_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down, one question at a time.")
        return

    chat_id = update.effective_chat.id
    if chat_id in _subscribers:
        _subscribers.discard(chat_id)
        _persist_subscribers()
        await update.message.reply_text(
            "Reminders off. You won't get session alerts or breaking news anymore.\n"
            "Use /notify again to turn them back on."
        )
    else:
        _subscribers.add(chat_id)
        _persist_subscribers()
        await update.message.reply_text(
            "Reminders on. You'll get:\n"
            "• 30-min alerts before each session\n"
            "• Qualifying, sprint and race results minutes after the flag\n"
        "• Breaking F1 news as it happens\n\n"
            "Use /notify again to turn them off."
        )
