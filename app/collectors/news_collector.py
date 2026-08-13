"""News & blogs — GDELT (free, keyless) + per-workspace RSS feeds. No approval,
no API key, works the moment this file runs."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
import httpx
import feedparser
from ..models import Workspace

log = logging.getLogger("collector.news")
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

def collect(db, ws: Workspace) -> list[dict]:
    return _gdelt(ws) + _rss(ws)

def _gdelt(ws: Workspace) -> list[dict]:
    try:
        r = httpx.get(GDELT_URL, params={"query": f'"{ws.name}"', "mode": "artlist",
                                         "format": "json", "maxrecords": 30, "timespan": "1d"}, timeout=30)
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
