"""app/services/competitor_scan.py — finds mentions of tracked competitors,
using the same real, working search mechanisms as the main brand collectors
(GDELT for news, Algolia for Hacker News), just pointed at a competitor's
name instead of the workspace's own keywords. Kept as its own small module
rather than bolted onto the existing collectors, since those are built
around a Workspace's own fields (brand_tokens, keywords, rss_feeds) — a
competitor is just a name, nothing else.
"""
from __future__ import annotations
import hashlib
import logging
import httpx
from datetime import datetime, timezone

from .classifier import classify_batch
from ..models import Competitor, CompetitorMention

log = logging.getLogger("competitor_scan")
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"


def _gdelt_for_term(term: str) -> list[dict]:
    try:
        r = httpx.get(GDELT_URL, params={"query": f'"{term}"', "mode": "artlist",
                                         "format": "json", "maxrecords": 20, "timespan": "7d"}, timeout=30)
        if r.status_code != 200:
            return []
        arts = r.json().get("articles", [])
    except Exception:  # noqa: BLE001
        log.exception("Competitor GDELT search failed for %r", term)
        return []
    out = []
    for a in arts:
        if not a.get("title"):
            continue
        out.append({"text": a["title"], "url": a.get("url", ""), "platform": "News"})
    return out


def _hn_for_term(term: str) -> list[dict]:
    try:
        r = httpx.get(HN_SEARCH_URL, params={"query": term, "tags": "story,comment", "hitsPerPage": 15}, timeout=20)
        r.raise_for_status()
        hits = r.json().get("hits", [])
    except Exception:  # noqa: BLE001
        log.exception("Competitor Hacker News search failed for %r", term)
        return []
    out = []
    for hit in hits:
        text = (hit.get("title") or hit.get("comment_text") or hit.get("story_text") or "").strip()
        if not text:
            continue
        object_id = hit.get("objectID", "")
        url = hit.get("url") or (f"https://news.ycombinator.com/item?id={object_id}" if object_id else "")
        out.append({"text": text[:1500], "url": url, "platform": "Hacker News"})
    return out


def scan_competitor(db, competitor: Competitor) -> dict:
    """Runs one competitor through both sources, classifies for sentiment
    only (competitors aren't escalated through Media Room, so severity
    doesn't apply here), and stores new mentions. Returns a small summary,
    the same shape as the main collector run summaries."""
    from sqlalchemy import select
    candidates = _gdelt_for_term(competitor.name) + _hn_for_term(competitor.name)
    if not candidates:
        return {"candidates": 0, "new": 0}

    classified = classify_batch(competitor.name, "Competitor comparison — sentiment only matters here.",
                                [{"idx": i, "text": c["text"], "platform": c["platform"]} for i, c in enumerate(candidates)])
    by_idx = {}
    for c in classified:
        try:
            by_idx[int(c["idx"])] = c
        except (TypeError, ValueError, KeyError):
            continue

    new_count = 0
    for i, cand in enumerate(candidates):
        content_hash = hashlib.sha256(cand["text"].encode("utf-8", "ignore")).hexdigest()
        existing = db.scalar(select(CompetitorMention).where(
            CompetitorMention.competitor_id == competitor.id, CompetitorMention.content_hash == content_hash))
        if existing:
            continue
        cls = by_idx.get(i) or {}
        db.add(CompetitorMention(
            competitor_id=competitor.id, workspace_id=competitor.workspace_id, text=cand["text"][:1500],
            url=cand.get("url", ""), platform=cand["platform"], sentiment=cls.get("sentiment", "Neutral"),
            content_hash=content_hash, posted_at=datetime.now(timezone.utc),
        ))
        new_count += 1
    db.commit()
    return {"candidates": len(candidates), "new": new_count}
