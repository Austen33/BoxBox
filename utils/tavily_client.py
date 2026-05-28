import asyncio
import logging
import os
from tavily import AsyncTavilyClient

logger = logging.getLogger(__name__)
_SEARCH_TIMEOUT_SECONDS = 15.0

_client = None

ALLOWED_DOMAINS = [
    "formula1.com",
    "fia.com",
    "autosport.com",
    "motorsport.com",
    "the-race.com",
    "racefans.net",
    "pitpass.com",
    "f1technical.net",
    "motorsportweek.com",
    "gpfans.com",
    "planetf1.com",
    "f1i.com",
    "grandprix247.com",
    "statsf1.com",
    "somersf1.com",
]


def get_tavily_client() -> AsyncTavilyClient:
    global _client
    if _client is None:
        _client = AsyncTavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    return _client


async def search(query: str, max_results: int = 5) -> list[dict]:
    """Run a Tavily search; return [] on timeout or any error."""
    try:
        client = get_tavily_client()
    except KeyError:
        logger.warning("TAVILY_API_KEY not set; skipping live search.")
        return []

    try:
        response = await asyncio.wait_for(
            client.search(
                query=query,
                search_depth="advanced",
                include_domains=ALLOWED_DOMAINS,
                max_results=max_results,
            ),
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Tavily search timed out for query: %s", query)
        return []
    except Exception as e:
        logger.warning("Tavily search failed for query '%s': %s", query, e)
        return []

    return response.get("results", [])


def format_search_results(results: list[dict]) -> str:
    if not results:
        return "No search results found."
    lines = []
    for r in results:
        lines.append(f"Source: {r.get('url', 'unknown')}")
        lines.append(f"Title: {r.get('title', '')}")
        lines.append(f"Content: {r.get('content', '')}")
        lines.append("")
    return "\n".join(lines)
