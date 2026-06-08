"""Shared aiohttp client + tiny TTL JSON cache.

Every Jolpi/Ergast read used to open and tear down its own
``aiohttp.ClientSession``. This module gives the whole bot one pooled session
and an in-memory TTL cache keyed by URL, so repeated reads (standings, results,
schedule, career paging) don't re-hit the rate-limited mirror within the TTL.

The session is created lazily on first use and closed on shutdown via
``close_session()`` (wired into the application's ``post_shutdown``).
"""

import logging
import time
from collections import OrderedDict

import aiohttp

logger = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None

# url -> (expires_at_monotonic, json). Bounded, LRU-ish eviction.
_CACHE_MAX = 256
_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()


def get_session() -> aiohttp.ClientSession:
    """Return the shared session, (re)creating it if needed."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session() -> None:
    """Close the shared session. Safe to call when none exists."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _cache_get(url: str):
    entry = _cache.get(url)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        _cache.pop(url, None)
        return None
    _cache.move_to_end(url)
    return value


def _cache_set(url: str, value: dict, ttl: float) -> None:
    _cache[url] = (time.monotonic() + ttl, value)
    _cache.move_to_end(url)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def clear_cache() -> None:
    _cache.clear()


async def get_json(url: str, ttl_seconds: float = 0, timeout: float = 15) -> dict | None:
    """GET ``url`` and return parsed JSON, or ``None`` on any failure/non-200.

    When ``ttl_seconds`` > 0 a successful response is cached and served from
    cache on subsequent calls within the TTL.
    """
    if ttl_seconds > 0:
        cached = _cache_get(url)
        if cached is not None:
            logger.debug("http cache HIT %s", url)
            return cached

    session = get_session()
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                logger.warning("http GET %s -> %s", url, resp.status)
                return None
            data = await resp.json()
    except Exception as e:
        logger.warning("http GET failed %s: %s", url, e)
        return None

    if ttl_seconds > 0 and data is not None:
        _cache_set(url, data, ttl_seconds)
    return data
