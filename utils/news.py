"""Rolling F1 + McLaren news digest, refreshed every few hours.

Injected into every conversation's system prompt so the bot knows what has
happened today and yesterday without needing a keyword to trigger a search.
Uses Tavily's news mode restricted to the last couple of days. Cached so the
Tavily quota isn't hit on every message.
"""

import asyncio
import logging
import time

from utils.tavily_client import get_tavily_client, ALLOWED_DOMAINS

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 3 * 3600
_QUERIES = [
    "McLaren F1 Norris Piastri",
    "Formula 1 news",
]
_cache: dict = {"ts": 0.0, "text": ""}
_lock = asyncio.Lock()


async def _fetch() -> str:
    client = get_tavily_client()
    results = await asyncio.gather(*[
        asyncio.wait_for(
            client.search(
                query=q, topic="news", days=2, search_depth="basic",
                max_results=8, include_domains=ALLOWED_DOMAINS,
            ),
            timeout=20,
        )
        for q in _QUERIES
    ], return_exceptions=True)

    seen, items = set(), []
    for res in results:
        if isinstance(res, Exception):
            logger.warning("news fetch failed: %s", res)
            continue
        for r in res.get("results", []):
            url = r.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            date = (r.get("published_date") or "")[:16]
            snippet = " ".join((r.get("content") or "").split())[:260]
            items.append(f"- [{date}] {r.get('title', '').strip()}: {snippet}")
    if not items:
        return ""
    return (
        "LATEST NEWS (last ~48h, from F1 news sites, refreshed every few hours). Treat as reported "
        "news, not official results; official results are in the LIVE DATA block:\n" + "\n".join(items[:14])
    )


def invalidate() -> None:
    """Force a refresh on the next request (e.g. right after a session ends).

    Keeps serving the current digest if the refresh fails."""
    _cache["ts"] = 0.0


async def latest_news() -> str:
    """Cached news digest; empty string if unavailable."""
    if time.time() - _cache["ts"] < REFRESH_SECONDS and _cache["text"]:
        return _cache["text"]
    async with _lock:
        if time.time() - _cache["ts"] < REFRESH_SECONDS and _cache["text"]:
            return _cache["text"]
        try:
            text = await _fetch()
        except KeyError:
            return ""  # no TAVILY_API_KEY
        except Exception:
            logger.warning("news digest failed", exc_info=True)
            text = ""
        # On failure keep serving the previous digest, retry in 15 minutes.
        if text:
            _cache.update(ts=time.time(), text=text)
        else:
            _cache["ts"] = time.time() - REFRESH_SECONDS + 900
        return _cache["text"]
