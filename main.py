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
        "/fantasy — F1 Fantasy picks for the upcoming round (top pick, value pick, constructor)\n"
        "/rumour \\[topic\\] — latest paddock rumours, flagged confirmed vs speculation\n"
        "/ask \\[question\\] — any F1 question, live search for recent stuff, knowledge base for history and tech\n\n"
        "You can also send a *voice note* and I'll transcribe it and answer like an /ask query.\n\n"
        "Lights out and away we go."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Welcome message and command list"),
        BotCommand("race", "Next race weekend countdown and session times"),
        BotCommand("predict", "Pre-race winner prediction"),
        BotCommand("strategy", "Post-race tyre strategy breakdown"),
        BotCommand("fantasy", "F1 Fantasy picks for the next round"),
        BotCommand("rumour", "Latest rumours about a driver or team"),
        BotCommand("ask", "Ask any F1 question"),
    ]
    await application.bot.set_my_commands(commands)


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
    application.add_handler(CommandHandler("race", race_handler))
    application.add_handler(CommandHandler("predict", predict_handler))
    application.add_handler(CommandHandler("strategy", strategy_handler))
    application.add_handler(CommandHandler("fantasy", fantasy_handler))
    application.add_handler(CommandHandler("rumour", rumour_handler))
    application.add_handler(CommandHandler("ask", ask_handler))
    application.add_handler(
        MessageHandler(filters.VOICE, voice_handler)
    )

    logger.info("BoxBox bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
