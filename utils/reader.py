"""Read a web page as clean text through Jina Reader (https://r.jina.ai).

Search results only carry a snippet; this fetches the whole article (or a page
a user links) so the model can answer from it. Jina does the fetching and
strips scripts and markup, returning plain text. No key is needed; set
JINA_API_KEY for higher rate limits.
"""

import logging
import os
from urllib.parse import urlparse

import aiohttp

from utils.http import get_session

logger = logging.getLogger(__name__)

_READER = "https://r.jina.ai/"
_TIMEOUT_SECONDS = 25
MAX_CHARS = 8000


async def read_page(url: str, max_chars: int = MAX_CHARS) -> str:
    """Return the page's text (trimmed to ``max_chars``), or a short error line."""
    url = url.strip()
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        return f"Not a web link: {url!r}."

    headers = {"Accept": "text/plain", "X-Return-Format": "text"}
    if key := os.environ.get("JINA_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"

    try:
        async with get_session().get(
            _READER + url, headers=headers, timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status != 200:
                logger.warning("Jina Reader %s -> %s", url, resp.status)
                return f"Couldn't read that page (status {resp.status})."
            text = await resp.text()
    except Exception as e:
        logger.warning("Jina Reader failed for %s: %s", url, e)
        return "Couldn't read that page (it timed out or refused the request)."

    text = text.strip()
    if not text:
        return "That page had no readable text."
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[Article cut off here]"
    return text
