import os
import logging
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from handlers.ask import ask_handler
from handlers.race import race_handler
from handlers.predict import predict_handler
from handlers.strategy import strategy_handler
from handlers.fantasy import fantasy_handler
from handlers.rumour import rumour_handler
from handlers.voice import voice_handler
from handlers.standings import standings_handler
from handlers.lap import lap_handler
from handlers.h2h import h2h_handler
from handlers.notify import notify_handler, setup_scheduler
from handlers.history import history_handler, career_handler
from handlers.rewind import rewind_handler
from handlers.result import result_handler
from handlers.profile import driver_handler, team_handler
from handlers.follow import follow_handler, unfollow_handler
from handlers.menu import menu_callback_handler
from handlers.mclaren_cmds import teammates_handler, title_handler, pace_handler, debrief_handler
from handlers.photo import photo_handler
from handlers.grid_cmd import grid_handler
from handlers.ask import chat_handler, reset_handler
from utils.telegram_safe import safe_reply
from utils.metrics import track

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

from utils import errorlog
errorlog.install()


async def testvoice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import io
    from telegram import InputFile
    from utils.groq_client import synthesize_speech
    from utils.admin import is_admin

    if not is_admin(update.effective_chat.id):
        return

    await update.message.reply_text("Testing TTS pipeline...")
    try:
        audio, fmt = await synthesize_speech("Verstappen takes pole. Ferrari are struggling on the mediums.")
        buf = io.BytesIO(audio)
        if fmt == "ogg":
            await update.message.reply_voice(voice=InputFile(buf, filename="test.ogg"))
        else:
            await update.message.reply_audio(audio=InputFile(buf, filename="test.mp3"), title="BoxBox")
        await update.message.reply_text(f"OK, {fmt}, {len(audio)} bytes.")
    except Exception as e:
        import traceback
        await update.message.reply_text(f"FAILED: {type(e).__name__}: {e}\n\n{traceback.format_exc()[-500:]}")


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*BoxBox*, your F1 race engineer in a bot\n\n"
        "Here's what I can do:\n\n"
        "/race - next race weekend countdown with all session times in Irish time\n"
        "/predict - pre-race winner prediction based on qualifying, form, and circuit data\n"
        "/strategy - post-race tyre strategy breakdown with optimal vs actual analysis\n"
        "/fantasy - F1 Fantasy picks for the upcoming round\n"
        "/standings - current drivers and constructors championship standings\n"
        "/rumour \\[topic\\] - latest paddock rumours, flagged confirmed vs speculation\n"
        "/ask \\[question\\] - any F1 question, live search for recent stuff\n"
        "/lap \\[driver\\] \\[session\\] - fastest lap summary (e.g. /lap VER Q)\n"
        "/h2h \\[driver1\\] \\[driver2\\] - head-to-head this season (e.g. /h2h VER NOR)\n"
        "/history \\[driver\\] \\[circuit\\] - driver's past results at a track\n"
        "/career \\[driver\\] - complete career statistics\n"
        "/driver \\[name\\] - driver profile card: season form and career stats\n"
        "/team \\[name\\] - team profile card: season standing and line-up\n"
        "/rewind \\[circuit\\] \\[year\\] - relive key moments from any past race\n"
        "/result - latest race result with concise DNF reasons\n"
        "/follow \\[driver/team\\] - flag their breaking news (also /unfollow)\n"
        "/grid - starting grid graphic for this weekend (penalties applied)\n"
        "/teammates - Norris vs Piastri: the McLaren team-mate battle\n"
        "/title - championship maths and what McLaren can still achieve\n"
        "/pace - McLaren race pace vs Mercedes, Ferrari and Red Bull (upgrade watch)\n"
        "/debrief - spoken McLaren debrief of the last race\n"
        "/reset - clear our conversation\n"
        "/notify - toggle session reminders, McLaren result alerts and breaking news\n"
        "\n"
        "Or just chat: type a question (follow-ups work), send a *voice note*, or send a *photo or screenshot* and ask about it.\n\n"
        "Lights out and away we go."
    )
    await safe_reply(update.message, text)


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: per-command call counts, errors, and average latency."""
    from utils.admin import is_admin
    from utils.metrics import format_stats

    if not is_admin(update.effective_chat.id):
        return
    await safe_reply(update.message, format_stats())


async def models_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: which models and APIs the bot is configured to use."""
    from utils.admin import get_admin_chat_id, is_admin
    from utils import groq_client as g

    chat_id = update.effective_chat.id
    if get_admin_chat_id() is not None and not is_admin(chat_id):
        await update.message.reply_text("/models is admin-only.")
        return

    def src(env: str) -> str:
        return "env" if os.environ.get(env) else "default"

    def has(env: str) -> str:
        return "key set" if os.environ.get(env) else "key MISSING"

    text = (
        "Models\n"
        f"Smart (chat, /ask, analysis): {g.SMART_MODEL} ({src('SMART_MODEL')})\n"
        f"Fast (short lookups, rewrites): {g.FAST_MODEL} ({src('FAST_MODEL')})\n"
        f"Vision (photos): {g.VISION_MODEL} ({src('VISION_MODEL')})\n"
        f"Speech-to-text: {g.STT_MODEL} ({src('STT_MODEL')})\n"
        f"Text-to-speech: edge-tts {g.EDGE_TTS_VOICE}, then {g.TTS_MODEL} "
        f"(voice {g.TTS_VOICE}), then gTTS\n\n"
        "APIs\n"
        f"LLM: OpenRouter, {g.OPENROUTER_URL} ({has('OPEN_ROUTER_KEY')})\n"
        f"Search: Tavily ({has('TAVILY_API_KEY')})"
    )
    await safe_reply(update.message, text, parse_mode=None)


async def errors_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin diagnostics: recent errors + config check, safe to paste into a chat.

    /errors [n]  show the last n errors (default 5)
    /errors clear  empty the log
    If no admin chat id is configured the log is shown to the caller (it is
    redacted), so you can debug a fresh deploy; set ADMIN_CHAT_ID to lock it down.
    """
    from utils.admin import get_admin_chat_id, is_admin
    from utils import errorlog

    chat_id = update.effective_chat.id
    admin_set = get_admin_chat_id() is not None
    if admin_set and not is_admin(chat_id):
        await update.message.reply_text(
            f"/errors is admin-only. Your chat id is {chat_id}, which doesn't match the "
            f"ADMIN_CHAT_ID / TELEGRAM_CHAT_ID set on the server. Set ADMIN_CHAT_ID={chat_id} "
            f"there and redeploy."
        )
        return

    arg = (context.args[0].lower() if context.args else "")
    if arg == "clear":
        errorlog.clear()
        await update.message.reply_text("Error log cleared.")
        return
    n = int(arg) if arg.isdigit() else 5
    text = errorlog.report(n)
    if not admin_set:
        text = (
            f"NOTE: no ADMIN_CHAT_ID set, so anyone can read this. Your chat id is {chat_id}; "
            f"set ADMIN_CHAT_ID={chat_id} to restrict /errors to you.\n\n" + text
        )
    await safe_reply(update.message, text, parse_mode=None)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled errors and tell the user something went wrong."""
    cmd = None
    user = None
    if isinstance(update, Update):
        if update.effective_message and update.effective_message.text:
            cmd = update.effective_message.text.split()[0]
        if update.effective_user:
            user = update.effective_user.id
    logger.error(
        "Unhandled exception (cmd=%s user=%s)", cmd, user, exc_info=context.error
    )
    try:
        if isinstance(update, Update) and update.effective_message is not None:
            err = context.error
            detail = errorlog.redact(f"{type(err).__name__}: {err}")[:300] if err else "unknown"
            await update.effective_message.reply_text(
                f"Something went wrong on my end. Try again in a moment.\n\nError: {detail}"
            )
    except Exception:
        pass


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Welcome message and command list"),
        BotCommand("race", "Next race weekend countdown and session times"),
        BotCommand("predict", "Pre-race winner prediction"),
        BotCommand("strategy", "Post-race tyre strategy breakdown"),
        BotCommand("fantasy", "F1 Fantasy picks for the next round"),
        BotCommand("standings", "Current championship standings"),
        BotCommand("rumour", "Latest rumours about a driver or team"),
        BotCommand("ask", "Ask any F1 question"),
        BotCommand("lap", "Fastest lap summary for a driver and session"),
        BotCommand("h2h", "Head-to-head stats for two drivers"),
        BotCommand("history", "Driver's past results at a circuit"),
        BotCommand("career", "Complete driver career statistics"),
        BotCommand("driver", "Driver profile card: season + career stats"),
        BotCommand("team", "Team profile card: season standing and line-up"),
        BotCommand("follow", "Follow a driver or team for flagged news"),
        BotCommand("unfollow", "Stop following a driver or team"),
        BotCommand("notify", "Toggle session reminders and breaking news"),
        BotCommand("rewind", "Relive key moments from a past race"),
        BotCommand("result", "Latest race result with DNF reasons"),
        BotCommand("grid", "Starting grid graphic with penalties"),
        BotCommand("teammates", "Norris vs Piastri team-mate battle"),
        BotCommand("title", "Championship maths for McLaren"),
        BotCommand("pace", "McLaren race pace and upgrade watch"),
        BotCommand("debrief", "Spoken McLaren debrief of the last race"),
        BotCommand("reset", "Clear our conversation"),
    ]
    await application.bot.set_my_commands(commands)
    setup_scheduler(application)


async def post_shutdown(application: Application) -> None:
    """Close the shared HTTP session on shutdown."""
    from utils.http import close_session
    await close_session()


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_TOKEN environment variable not set")

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Each command is wrapped with track() so it emits a timing/outcome log
    # line and feeds the admin /stats counters.
    commands = {
        "start": start_handler,
        "help": start_handler,
        "race": race_handler,
        "predict": predict_handler,
        "strategy": strategy_handler,
        "fantasy": fantasy_handler,
        "rumour": rumour_handler,
        "ask": ask_handler,
        "standings": standings_handler,
        "lap": lap_handler,
        "h2h": h2h_handler,
        "history": history_handler,
        "career": career_handler,
        "notify": notify_handler,
        "rewind": rewind_handler,
        "result": result_handler,
        "driver": driver_handler,
        "team": team_handler,
        "follow": follow_handler,
        "unfollow": unfollow_handler,
        "grid": grid_handler,
        "teammates": teammates_handler,
        "title": title_handler,
        "pace": pace_handler,
        "debrief": debrief_handler,
        "reset": reset_handler,
    }
    for name, handler in commands.items():
        application.add_handler(CommandHandler(name, track(name)(handler)))

    # Admin/diagnostic commands (hidden from the public command menu).
    application.add_handler(CommandHandler("testvoice", testvoice_handler))
    application.add_handler(CommandHandler("stats", stats_handler))
    application.add_handler(CommandHandler("errors", errors_handler))
    application.add_handler(CommandHandler("models", models_handler))

    application.add_handler(
        MessageHandler(filters.VOICE, track("voice")(voice_handler))
    )
    application.add_handler(
        MessageHandler(filters.PHOTO, track("photo")(photo_handler))
    )
    # Plain text in private chats is a normal conversation (with memory).
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            track("chat")(chat_handler),
        )
    )
    # Race-weekend hub inline buttons (callback_data starts with "hub:").
    application.add_handler(
        CallbackQueryHandler(track("hub")(menu_callback_handler), pattern=r"^hub:")
    )
    application.add_error_handler(error_handler)

    logger.info("BoxBox bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
