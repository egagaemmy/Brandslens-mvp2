"""X/Twitter — OFF by default (see config.X_ENABLED). Built and ready; flip on
the day it's funded ($200/mo X API Basic minimum for usable search access) —
nothing else in the app changes when you do."""
from __future__ import annotations
import logging
from datetime import datetime
import httpx
from ..config import X_ENABLED, X_BEARER_TOKEN
from ..models import Workspace

log = logging.getLogger("collector.x")
SEARCH_URL = "https://api.x.com/2/tweets/search/recent"

def collect(db, ws: Workspace) -> list[dict]:
    if not X_ENABLED:
        return []  # deliberately silent — this is an expected, budgeted-for gap at MVP stage
    if not X_BEARER_TOKEN:
        log.warning("X_ENABLED=true but X_BEARER_TOKEN missing")
        return []
    brand = f'"{ws.name}"' if " " in ws.name else ws.name
    terms = [f'"{t}"' for t in (ws.keywords or [])[:15]]
    query = "(" + " OR ".join([brand] + terms) + ") -is:retweet"
    try:
        r = httpx.get(SEARCH_URL, headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
                     params={"query": query[:1024], "max_results": 50,
                            "tweet.fields": "created_at,public_metrics",
                            "expansions": "author_id", "user.fields": "username,public_metrics"}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception:  # noqa: BLE001
        log.exception("X search failed for %s", ws.name)
        return []
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    out = []
    for t in data.get("data", []):
        u = users.get(t.get("author_id"), {})
        followers = u.get("public_metrics", {}).get("followers_count", 0)
        out.append({"text": t["text"], "url": f"https://x.com/{u.get('username','i')}/status/{t['id']}",
                   "author": "@" + u.get("username", "unknown"), "platform": "X/Twitter",
                   "posted_at": datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")) if t.get("created_at") else None,
                   "reach": followers})
    return out
