"""
# Google Scholar

Recent papers on a topic, so a forum post can cite current research.

## 1. What This Module Does

`search` sends one query to scholar.google.com (optionally from a given
year onwards) and returns its first results: title, link, authors and venue
line, snippet and citation count.

## 2. Why This Module Exists

The forum agent draws on the student's course knowledge, which stops where
the slides stop. Scholar tells it what has been published since, so a reply
can point another agent to a concrete recent paper instead of a vague claim.
Scholar has no API, so this reads the results page as a browser would.

## 3. How It Fits in the Architecture

Called only by `mitsync forum scholar`, which only the forum agent's
wrapper exposes. It is a GET to one fixed host and never sends a Canvas
credential. The agent's instructions allow at most two searches per run,
which keeps it well under anything Scholar would treat as scraping.

## 4. Key Concepts

**Results are leads, not facts.** A snippet is a fragment of an abstract.
The agent may cite a title, venue and year as Scholar shows them, and must
not claim more than the snippet says.

**Why exceptions are caught here.** Scholar is an external system that
answers abuse checks with a CAPTCHA page (often with status 200) or a 429. Both
become `ScholarBlocked`, which says to go on without papers this run.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from mitsync.core.errors import ScholarBlocked

__all__ = ["search"]

SCHOLAR = "https://scholar.google.com/scholar"
#: Scholar serves its plain results page to a browser; an httpx user agent gets a CAPTCHA.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def search(
    query: str,
    since: int | None = None,
    limit: int = 8,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """The first `limit` Scholar results for `query`, from `since` onwards if given."""
    params: dict[str, Any] = {"q": query, "hl": "en"}
    if since:
        params["as_ylo"] = since
    with httpx.Client(transport=transport, headers=_HEADERS, timeout=20) as web:
        try:
            response = web.get(SCHOLAR, params=params, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ScholarBlocked(
                f"Google Scholar did not answer ({exc}); go on without papers"
            ) from exc
    soup = BeautifulSoup(response.text, "html.parser")
    if soup.select_one("#gs_captcha_ccl, #captcha-form") or "unusual traffic" in response.text:
        raise ScholarBlocked("Google Scholar asked for a CAPTCHA; go on without papers this run")
    results = []
    for block in soup.select("div.gs_ri")[:limit]:
        title = block.select_one("h3.gs_rt")
        link = title.select_one("a") if title else None
        cited = block.find("a", string=re.compile(r"^Cited by \d+"))
        meta = block.select_one("div.gs_a")
        snippet = block.select_one("div.gs_rs")
        results.append(
            {
                "title": re.sub(r"^\[[A-Z]+\]\s*", "", title.get_text(" ", strip=True))
                if title
                else "",
                "url": link["href"] if link else None,
                "authors_venue_year": meta.get_text(" ", strip=True) if meta else "",
                "snippet": " ".join(snippet.get_text(" ", strip=True).split()) if snippet else "",
                "cited_by": int(cited.get_text().split()[-1]) if cited else 0,
            }
        )
    return results
