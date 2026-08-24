"""News & blogs — GDELT (free, keyless) + per-workspace RSS feeds. No approval,
no API key, works the moment this file runs."""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone
import httpx
import feedparser
from ..models import Workspace
from ..services.pipeline import search_terms

log = logging.getLogger("collector.news")
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

def collect(db, ws: Workspace, days_back: int | None = None) -> list[dict]:
    return _gdelt(ws, days_back=days_back) + _rss(ws)

def _gdelt(ws: Workspace, _attempt: int = 1, days_back: int | None = None) -> list[dict]:
    terms = search_terms(ws)
    query = "(" + " OR ".join(f'"{t}"' for t in terms) + ")"
    timespan = f"{days_back}d" if days_back else "1d"
    try:
        r = httpx.get(GDELT_URL, params={"query": query, "mode": "artlist",
                                         "format": "json", "maxrecords": 30, "timespan": timespan}, timeout=30)
        if r.status_code == 429:
            # GDELT is free and keyless, which means it's also more
            # aggressively rate-limited than a paid/keyed API. This happened
            # for real in production once there were multiple workspaces
            # running back-to-back — wait it out once, then give up cleanly
            # rather than raising, since the next scheduled run will pick
            # this workspace up again regardless.
            if _attempt < 3:
                wait = 5 * _attempt
                log.warning("GDELT rate-limited for %s — waiting %ss (attempt %s/3)", ws.name, wait, _attempt)
                time.sleep(wait)
                return _gdelt(ws, _attempt + 1, days_back=days_back)
            log.warning("GDELT still rate-limited for %s after 3 attempts — skipping this run", ws.name)
            return []
        r.raise_for_status()
        arts = r.json().get("articles", [])
    except Exception:  # noqa: BLE001
        log.exception("GDELT failed for %s", ws.name)
        return []
    out = []
    for a in arts:
        if not a.get("title"):
            continue
        out.append({"text": a["title"], "url": a.get("url", ""), "author": a.get("domain", ""),
                    "platform": "News", "posted_at": _ts(a.get("seendate")), "reach": 0})
    return out

def _ts(s):
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None

def _rss(ws: Workspace) -> list[dict]:
    out = []
    for url in (ws.rss_feeds or []):
        try:
            parsed = feedparser.parse(url)
            for e in parsed.entries[:25]:
                text = f"{e.get('title','')}. {e.get('summary','')[:400]}".strip()
                out.append({"text": text, "url": e.get("link", ""), "author": parsed.feed.get("title", url),
                           "platform": "Blog" if "blog" in url.lower() else "News", "posted_at": None, "reach": 0})
        except Exception:  # noqa: BLE001
            log.exception("RSS failed: %s", url)
    return out
