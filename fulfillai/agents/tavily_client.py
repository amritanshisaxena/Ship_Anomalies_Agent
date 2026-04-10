"""
Tavily Search API wrapper + scope-aware grounding query builder.

Used by agents/investigator.py to pull real web search results for an anomaly
before asking the LLM to diagnose it. This is the anti-hallucination layer:
the LLM is instructed to reason only over internal pipeline facts and these
search results.

If TAVILY_API_KEY is missing, tavily_search() returns an empty list and the
investigator records a low-confidence diagnosis rather than inventing a cause.
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
TAVILY_URL = "https://api.tavily.com/search"
REQUEST_TIMEOUT = 12.0
MAX_SNIPPET_CHARS = 500


async def tavily_search(query: str, max_results: int = 5) -> list[dict]:
    """
    Call the Tavily Search API for one query.

    Returns a list of {title, url, content, published_date, score}.
    Returns an empty list on any failure (no key, timeout, HTTP error, bad JSON).
    Never raises — the caller treats empty results as "no grounding".
    """
    if not TAVILY_API_KEY:
        return []

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.post(
                TAVILY_URL,
                json={
                    "api_key": TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                },
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        print(f"[tavily] search failed for {query!r}: {exc}")
        return []

    results = []
    for item in data.get("results", []):
        results.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": item.get("url") or "",
                "content": (item.get("content") or "")[:MAX_SNIPPET_CHARS],
                "published_date": item.get("published_date") or "",
                "score": item.get("score") or 0,
            }
        )
    return results


def build_grounding_queries(anomaly, fc=None, carrier=None) -> list[str]:
    """
    Build a small set of scope-aware search queries for an anomaly.

    The current month+year is included to bias Tavily toward recent results.
    """
    today = datetime.utcnow().strftime("%B %Y")
    queries: list[str] = []

    scope_type = anomaly.scope_type

    if scope_type == "fc" and fc is not None:
        city = fc.city or ""
        state = fc.state or ""
        queries.append(f"severe weather {city} {state} {today}")
        queries.append(f"{city} {state} shipping disruption OR airport delays {today}")
        queries.append(f"natural disaster OR wildfire OR flood OR storm {city} {today}")

    elif scope_type == "carrier" and carrier is not None:
        name = carrier.name or ""
        queries.append(f"{name} delivery delays OR outage OR strike {today}")
        queries.append(f"{name} service disruption {today}")

    elif scope_type == "region":
        region = (anomaly.scope_label or "").replace(" region", "").strip() or "US"
        queries.append(f"severe weather {region} United States {today}")
        queries.append(f"natural disaster OR storm {region} {today}")

    else:
        # Single-order or unknown scope — just a general query
        queries.append(f"shipping delays {today}")

    # Filter empties and dedupe while preserving order
    seen = set()
    final = []
    for q in queries:
        q = " ".join(q.split())
        if q and q not in seen:
            seen.add(q)
            final.append(q)
    return final
