import os
from groq import AsyncGroq

client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])

FAST_MODEL = "llama-3.1-8b-instant"
SMART_MODEL = "llama-3.3-70b-versatile"
WHISPER_MODEL = "whisper-large-v3-turbo"

SYSTEM_PROMPT = """You are BoxBox, a Telegram F1 bot. You are a knowledgeable mate who follows F1 obsessively.

Rules for every response:
- Write in plain, natural English. No textbook tone, no news article style.
- Never use em dashes as punctuation.
- Never use phrases like "it is worth noting", "dive into", "certainly", "delve", "it is important to note", "fascinatingly", "it's worth mentioning", "needless to say".
- Avoid unnecessary bullet lists. Use prose unless a list genuinely helps the reader.
- Technical explanations should feel like a race engineer talking to a smart fan who wants to actually understand something, not just get a surface level answer.
- Always be factual. If something is uncertain, say so clearly.
- Keep responses concise but complete. Do not pad answers with filler sentences.
- Format for Telegram: use *bold* and _italic_ sparingly where it genuinely helps, keep paragraphs short.
- Never recommend drivers or teams based on memory alone. Always treat driver and constructor information as potentially outdated and rely on the search context provided."""


async def chat(messages: list, model: str = SMART_MODEL, system: str = SYSTEM_PROMPT) -> str:
    full_messages = [{"role": "system", "content": system}] + messages
    response = await client.chat.completions.create(
        model=model,
        messages=full_messages,
        temperature=0.7,
        max_tokens=1024,
    )
    return response.choices[0].message.content


async def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    transcription = await client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=WHISPER_MODEL,
        response_format="text",
    )
    return transcription
