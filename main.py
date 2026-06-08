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
from handlers.menu import menu_callback_handler
from utils.telegram_safe import safe_reply
from utils.metrics import track

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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
        await update.message.reply_text(f"OK — {fmt}, {len(audio)} bytes.")
    except Exception as e:
        import traceback
        await update.message.reply_text(f"FAILED: {type(e).__name__}: {e}\n\n{traceback.format_exc()[-500:]}")


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*BoxBox* — your F1 race engineer in a bot\n\n"
        "Here's what I can do:\n\n"
        "/race — next race weekend countdown with all session times in Irish time\n"
        "/predict — pre-race winner prediction based on qualifying, form, and circuit data\n"
        "/strategy — post-race tyre strategy breakdown with optimal vs actual analysis\n"
        "/fantasy — F1 Fantasy picks for the upcoming round\n"
        "/standings — current drivers and constructors championship standings\n"
        "/rumour \\[topic\\] — latest paddock rumours, flagged confirmed vs speculation\n"
        "/ask \\[question\\] — any F1 question, live search for recent stuff\n"
        "/lap \\[driver\\] \\[session\\] — fastest lap summary (e.g. /lap VER Q)\n"
        "/h2h \\[driver1\\] \\[driver2\\] — head-to-head this season (e.g. /h2h VER NOR)\n"
        "/history \\[driver\\] \\[circuit\\] — driver's past results at a track\n"
        "/career \\[driver\\] — complete career statistics\n"
        "/rewind \\[circuit\\] \\[year\\] — relive key moments from any past race\n"
        "/result — latest race result with concise DNF reasons\n"
        "/notify — toggle session reminders and breaking news alerts\n"
        "\n"
        "You can also send a *voice note* and I'll transcribe it and answer like an /ask query.\n\n"
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
            await update.effective_message.reply_text(
                "Something went wrong on my end. Try again in a moment."
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
        BotCommand("notify", "Toggle session reminders and breaking news"),
        BotCommand("rewind", "Relive key moments from a past race"),
        BotCommand("result", "Latest race result with DNF reasons"),
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
    }
    for name, handler in commands.items():
        application.add_handler(CommandHandler(name, track(name)(handler)))

    # Admin/diagnostic commands (hidden from the public command menu).
    application.add_handler(CommandHandler("testvoice", testvoice_handler))
    application.add_handler(CommandHandler("stats", stats_handler))

    application.add_handler(
        MessageHandler(filters.VOICE, track("voice")(voice_handler))
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
