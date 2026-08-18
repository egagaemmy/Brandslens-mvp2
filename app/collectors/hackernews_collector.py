"""Hacker News — via the Algolia HN Search API. Completely free, no key, no
account, no approval process of any kind — the most frictionless source in
this whole system after GDELT itself. Particularly valuable for tech,
fintech, and startup-adjacent workspaces, since that conversation
concentrates here more than almost anywhere else public.

Like Nairaland, this API has no boolean OR syntax for combining multiple
search phrases into one request, so — same pattern as that collector — this
makes one request per tracked term rather than combining them, capped to a
small number to keep each scan fast and considerate of a free public API.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
import httpx
from ..models import Workspace
from ..services.pipeline import search_terms

log = logging.getLogger("collector.hackernews")
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
MAX_TERMS_PER_RUN = 5


def collect(db, ws: Workspace) -> list[dict]:
    out, seen = [], set()
    for term in search_terms(ws, limit=MAX_TERMS_PER_RUN):
        out.extend(_search_one(term, seen))
    return out


def _search_one(term: str, seen: set[str]) -> list[dict]:
    try:
        r = httpx.get(HN_SEARCH_URL, params={"query": term, "tags": "story,comment", "hitsPerPage": 20}, timeout=20)
        r.raise_for_status()
        hits = r.json().get("hits", [])
    except Exception:  # noqa: BLE001
        log.exception("Hacker News search failed for %r", term)
        return []

    out = []
    for hit in hits:
        object_id = hit.get("objectID", "")
        if not object_id or object_id in seen:
            continue
        text = (hit.get("title") or hit.get("comment_text") or hit.get("story_text") or "").strip()
        if not text:
            continue
        seen.add(object_id)
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
        out.append({
            "text": text[:1500],
            "url": url,
            "author": hit.get("author", "unknown"),
            "platform": "Hacker News",
            "posted_at": _parse_date(hit.get("created_at")),
            "reach": hit.get("points") or 0,
        })
    return out


def _parse_date(raw: str | None):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
