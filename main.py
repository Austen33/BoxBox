import os
import logging
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
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

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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
        "/notify — toggle session reminders and breaking news alerts\n"
        "\n"
        "You can also send a *voice note* and I'll transcribe it and answer like an /ask query.\n\n"
        "Lights out and away we go."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled errors and tell the user something went wrong."""
    logger.error("Unhandled exception while handling update", exc_info=context.error)
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
    ]
    await application.bot.set_my_commands(commands)
    setup_scheduler(application)


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_TOKEN environment variable not set")

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", start_handler))
    application.add_handler(CommandHandler("race", race_handler))
    application.add_handler(CommandHandler("predict", predict_handler))
    application.add_handler(CommandHandler("strategy", strategy_handler))
    application.add_handler(CommandHandler("fantasy", fantasy_handler))
    application.add_handler(CommandHandler("rumour", rumour_handler))
    application.add_handler(CommandHandler("ask", ask_handler))
    application.add_handler(CommandHandler("standings", standings_handler))
    application.add_handler(CommandHandler("lap", lap_handler))
    application.add_handler(CommandHandler("h2h", h2h_handler))
    application.add_handler(CommandHandler("history", history_handler))
    application.add_handler(CommandHandler("career", career_handler))
    application.add_handler(CommandHandler("notify", notify_handler))
    application.add_handler(CommandHandler("rewind", rewind_handler))
    application.add_handler(
        MessageHandler(filters.VOICE, voice_handler)
    )
    application.add_error_handler(error_handler)

    logger.info("BoxBox bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
