import os
from tavily import AsyncTavilyClient

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
    client = get_tavily_client()
    response = await client.search(
        query=query,
        search_depth="advanced",
        include_domains=ALLOWED_DOMAINS,
        max_results=max_results,
    )
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
