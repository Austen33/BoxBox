"""Official FIA stewards' documents for the current race weekend.

The FIA's F1 documents page lists every decision, infringement, summons and
classification PDF for the latest event. This scrapes that list and has the
model read the PDFs directly, so penalties come from the source instead of
news write-ups.
"""

import asyncio
import html
import logging
import re
import time

import aiohttp

from utils.http import get_session

logger = logging.getLogger(__name__)

BASE = "https://www.fia.com"
DOCS_URL = f"{BASE}/documents/championships/fia-formula-one-world-championship-14"
_HEADERS = {"User-Agent": "Mozilla/5.0 (BoxBox F1 bot)"}
LIST_TTL = 10 * 60
_MAX_PDF_BYTES = 8 * 1024 * 1024

_list_cache: dict = {"ts": 0.0, "data": None}
_pdf_cache: dict[str, bytes] = {}
_lock = asyncio.Lock()

_ROW_RE = re.compile(r'<li class="document-row[^"]*">(.*?)</li>', re.S)
_HREF_RE = re.compile(r'href="([^"]+\.pdf)"')
_TITLE_RE = re.compile(r'class="title">(.*?)</div>', re.S)
_DATE_RE = re.compile(r'date-display-single">([^<]+)<')
_EVENT_RE = re.compile(r'class="event-title active">([^<]+)<')


def _clean(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _parse(page: str) -> dict:
    docs = []
    for row in _ROW_RE.findall(page):
        href, title = _HREF_RE.search(row), _TITLE_RE.search(row)
        if not href or not title:
            continue
        text = _clean(title.group(1))
        m = re.match(r"Doc (\d+)\s*-\s*(.*)", text)
        date = _DATE_RE.search(row)
        docs.append({
            "num": int(m.group(1)) if m else 0,
            "title": m.group(2) if m else text,
            "url": BASE + href.group(1) if href.group(1).startswith("/") else href.group(1),
            "published": date.group(1).strip() + " CET" if date else "",
        })
    event = _EVENT_RE.search(page)
    docs.sort(key=lambda d: d["num"], reverse=True)
    return {"event": _clean(event.group(1)) if event else "", "docs": docs}


async def list_documents() -> dict:
    """{"event": name, "docs": [{"num", "title", "url", "published"}]}, newest first."""
    if _list_cache["data"] and time.time() - _list_cache["ts"] < LIST_TTL:
        return _list_cache["data"]
    async with _lock:
        if _list_cache["data"] and time.time() - _list_cache["ts"] < LIST_TTL:
            return _list_cache["data"]
        try:
            async with get_session().get(
                DOCS_URL, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                resp.raise_for_status()
                data = _parse(await resp.text())
        except Exception as e:
            logger.warning("FIA document list failed: %s", e)
            return _list_cache["data"] or {"event": "", "docs": []}
        _list_cache.update(ts=time.time(), data=data)
        return data


async def fetch_pdf(url: str) -> bytes | None:
    if url in _pdf_cache:
        return _pdf_cache[url]
    try:
        async with get_session().get(
            url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            body = await resp.read()
    except Exception as e:
        logger.warning("FIA PDF fetch failed %s: %s", url, e)
        return None
    if not body.startswith(b"%PDF") or len(body) > _MAX_PDF_BYTES:
        return None
    if len(_pdf_cache) > 200:
        _pdf_cache.clear()
    _pdf_cache[url] = body  # published documents don't change
    return body


async def pdf_parts(docs: list[dict]) -> list[dict]:
    """OpenRouter file parts for ``docs`` (skipping any that fail to download)."""
    from utils.groq_client import pdf_part

    bodies = await asyncio.gather(*(fetch_pdf(d["url"]) for d in docs))
    return [
        pdf_part(body, f"Doc {d['num']} - {d['title']}.pdf"[:120])
        for d, body in zip(docs, bodies) if body
    ]


def matches(event: str, race_name: str) -> bool:
    """Loose match between the FIA's event name and a Jolpi race name."""
    a = set(re.findall(r"[a-z]+", event.lower())) - {"grand", "prix", "formula", "the", "in"}
    b = set(re.findall(r"[a-z]+", race_name.lower())) - {"grand", "prix", "formula", "the", "in"}
    return bool(a & b)


def format_list(data: dict, filter_text: str = "", limit: int = 60) -> str:
    docs = data["docs"]
    if filter_text:
        words = filter_text.lower().split()
        docs = [d for d in docs if all(w in d["title"].lower() for w in words)]
    if not docs:
        return f"No FIA documents{' matching ' + repr(filter_text) if filter_text else ''} for {data['event'] or 'the current event'}."
    lines = [f"FIA documents, {data['event']} (newest first):"]
    lines += [f"Doc {d['num']}: {d['title']} ({d['published']})" for d in docs[:limit]]
    return "\n".join(lines)


_SUMMARY_PROMPT = """This is an official FIA Formula 1 document ({title}). Write down its substance as compact
plain text, so questions can be answered later without the PDF:
- Decision, infringement or summons: car number, driver and team, session and time, the alleged offence and
  regulation, the decision (penalty, or no further action) and the stewards' reasons, concisely but completely.
- Classification or starting grid: every row (position, car number, driver, team, and laps, time, gap or status),
  plus any notes such as penalties applied or deleted lap times.
- Anything else: the key facts and decisions.
No commentary and nothing that isn't in the document."""

_SUMMARY_KEY = "fia_summaries_v1"
_MAX_SUMMARIES = 400
_summaries: dict[str, str] | None = None
_inflight: dict[str, asyncio.Task] = {}


def _load_summaries() -> dict[str, str]:
    global _summaries
    if _summaries is None:
        from utils import store
        data = store.load(_SUMMARY_KEY, {})
        _summaries = data if isinstance(data, dict) else {}
    return _summaries


async def _summarise(doc: dict) -> str:
    from utils import store
    from utils.groq_client import SMART_MODEL, chat

    parts = await pdf_parts([doc])
    if not parts:
        return ""
    text = await chat(
        messages=[{"role": "user", "content": [
            {"type": "text", "text": _SUMMARY_PROMPT.format(title=doc["title"])}, *parts]}],
        model=SMART_MODEL,
        system="You read FIA stewards' documents accurately and write them down plainly.",
        temperature=0,
    )
    if text:
        cache = _load_summaries()
        cache[doc["url"]] = text
        while len(cache) > _MAX_SUMMARIES:
            cache.pop(next(iter(cache)))
        store.save(_SUMMARY_KEY, cache)
    return text


async def summary(doc: dict) -> str:
    """Text summary of one document. Each document is read by the model once,
    then served from the persistent cache (published documents never change)."""
    cached = _load_summaries().get(doc["url"])
    if cached:
        return cached
    task = _inflight.get(doc["url"])
    if task is None:
        task = asyncio.create_task(_summarise(doc))
        _inflight[doc["url"]] = task
        task.add_done_callback(lambda _t, url=doc["url"]: _inflight.pop(url, None))
    return await task


async def summaries_text(docs: list[dict]) -> str:
    texts = await asyncio.gather(*(summary(d) for d in docs), return_exceptions=True)
    blocks = []
    for d, t in zip(docs, texts):
        if isinstance(t, Exception) or not t:
            logger.warning("FIA summary failed for doc %s: %s", d["num"], t)
            t = "(couldn't read this document)"
        blocks.append(f"Doc {d['num']} - {d['title']} ({d['published']}):\n{t}")
    return "\n\n".join(blocks)


async def read_documents(doc_numbers: list[int]) -> str:
    """Summaries of the given documents from the current event."""
    data = await list_documents()
    wanted = [d for d in data["docs"] if d["num"] in set(doc_numbers)][:10]
    if not wanted:
        return "None of those document numbers are on the FIA page for this event."
    return f"FIA documents, {data['event']}:\n\n" + await summaries_text(wanted)
