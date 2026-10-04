import logging

from telegram import Update
from telegram.ext import ContextTypes

from handlers.ask import user_context
from utils import convo
from utils.f1_tools import TOOL_RULES, f1_tools
from utils.groq_client import VIDEO_MODEL, VISION_MODEL, chat_attachment, image_part, pdf_part, video_part
from utils.http import get_session
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import stream_reply

logger = logging.getLogger(__name__)

_DEFAULT_QUESTION = "What's going on in this image, and what does it mean for the race or for McLaren?"
_DEFAULT_PDF_QUESTION = "Summarise this document and what it means for the race or for McLaren."

_PROMPT = """The user sent an image (a screenshot, photo, timing screen, graphic or social post) with this message: {question}

Describe only what you can actually see, then answer. Read any text, numbers, timings and team colours carefully
and say if something is too small or unclear to read. Do not guess driver identities from faces; use captions,
numbers, names and team liveries visible in the image. For current results, standings or anything else not in
the image, use your tools rather than guessing. Keep it concise.

Particular kinds of image:
- Timing screen or live gaps (F1 TV, the F1 app, a TV graphic): read positions, gaps, intervals, tyre compounds
  and ages carefully, then answer with strategy in mind: who is in the pit window (a stop costs roughly 20 to 25
  seconds at most tracks), who is under undercut threat, and who is on old tyres.
- F1 Fantasy team screenshot: read the drivers, constructors, prices, budget left and any chips or boosts. Save
  the team with remember_about_user (key fantasy_team) so /fantasy can use it later, then give transfer advice for
  the next race using the data tools and web_search for form and value.
If the image and question have nothing to do with Formula 1 or motorsport, don't describe or answer it;
reply in one sentence that you're an F1 bot and only answer F1 questions."""

_PDF_PROMPT = """The user sent a PDF ({filename}) with this message: {question}

Read it carefully and answer from the document. If it's an FIA document (stewards' decision, classification,
technical directive), say what it decides and why it matters. Use your tools for anything the document doesn't
cover. Keep it concise. If it has nothing to do with Formula 1 or motorsport, reply in one sentence that you're an
F1 bot and only answer F1 questions."""

_DEFAULT_VIDEO_QUESTION = "What happens in this clip, and what does it mean for the race or for McLaren?"

_VIDEO_PROMPT = """The user sent a video clip (an onboard, a replay, a broadcast clip or a social post) with this message: {question}

Describe only what you can actually see and hear, then answer. Identify cars by livery, car number and any on-screen
graphics, not by guesswork. For incidents, say who was where and what happened, and give a view on likely steward
action without stating it as fact. For current results, standings or anything else not in the clip, use your tools.
Keep it concise. If the clip has nothing to do with Formula 1 or motorsport, don't describe it; reply in one sentence
that you're an F1 bot and only answer F1 questions."""

_MAX_PDF_BYTES = 15 * 1024 * 1024
_MAX_VIDEO_BYTES = 20 * 1024 * 1024  # Telegram's bot download limit


async def _download(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    file = await context.bot.get_file(file_id)
    async with get_session().get(file.file_path) as resp:
        resp.raise_for_status()
        return await resp.read()


async def _answer(update: Update, prompt: str, attachment: dict, remembered_as: str,
                  model: str = VISION_MODEL) -> None:
    msg = update.message
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    response = await stream_reply(msg, lambda on_text: chat_attachment(
        user_context(user_id) + prompt, attachment,
        history=convo.get_history(chat_id), model=model,
        tools=f1_tools(user_id), extra_system=TOOL_RULES,
        effort="medium", on_text=on_text,
    ))
    convo.add_exchange(chat_id, remembered_as, response)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photos, and images sent as files (uncompressed screenshots)."""
    msg = update.message
    if not msg:
        return
    if msg.photo:
        file_id, mime = msg.photo[-1].file_id, "image/jpeg"  # largest size
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id, mime = msg.document.file_id, msg.document.mime_type
    else:
        return
    if is_rate_limited(update.effective_user.id):
        await msg.reply_text("Slow down, one question at a time.")
        return

    await msg.reply_chat_action("typing")
    try:
        image = await _download(context, file_id)
        question = (msg.caption or "").strip() or _DEFAULT_QUESTION
        await _answer(update, _PROMPT.format(question=question), image_part(image, mime),
                      f"[sent an image] {question}")
    except Exception:
        logger.exception("Photo handler error")
        await msg.reply_text("Couldn't read that image. Try sending it again, or ask in text.")


async def pdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PDFs (FIA decisions, technical directives...) read directly by the model."""
    msg = update.message
    if not msg or not msg.document:
        return
    if is_rate_limited(update.effective_user.id):
        await msg.reply_text("Slow down, one question at a time.")
        return
    if (msg.document.file_size or 0) > _MAX_PDF_BYTES:
        await msg.reply_text("That PDF is too big for me, send one under 15 MB.")
        return

    await msg.reply_chat_action("typing")
    try:
        body = await _download(context, msg.document.file_id)
        filename = msg.document.file_name or "document.pdf"
        question = (msg.caption or "").strip() or _DEFAULT_PDF_QUESTION
        await _answer(update, _PDF_PROMPT.format(filename=filename, question=question),
                      pdf_part(body, filename), f"[sent a PDF: {filename}] {question}")
    except Exception:
        logger.exception("PDF handler error")
        await msg.reply_text("Couldn't read that PDF. Try sending it again.")


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Video clips, round video notes, GIFs and videos sent as files, read by VIDEO_MODEL."""
    msg = update.message
    if not msg:
        return
    clip = msg.video or msg.video_note or msg.animation or msg.document
    if clip is None:
        return
    if is_rate_limited(update.effective_user.id):
        await msg.reply_text("Slow down, one question at a time.")
        return
    if (clip.file_size or 0) > _MAX_VIDEO_BYTES:
        await msg.reply_text("That clip is too big for me, send one under 20 MB.")
        return

    await msg.reply_chat_action("typing")
    try:
        body = await _download(context, clip.file_id)
        mime = getattr(clip, "mime_type", None) or "video/mp4"
        question = (msg.caption or "").strip() or _DEFAULT_VIDEO_QUESTION
        await _answer(update, _VIDEO_PROMPT.format(question=question), video_part(body, mime),
                      f"[sent a video clip] {question}", model=VIDEO_MODEL)
    except Exception:
        logger.exception("Video handler error")
        await msg.reply_text("Couldn't watch that clip. Try a shorter one, or ask in text.")
