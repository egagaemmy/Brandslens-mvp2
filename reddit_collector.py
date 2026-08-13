"""Reddit — free API tier, instant approval (register an app at
reddit.com/prefs/apps, no review). Uses OAuth client-credentials flow."""
from __future__ import annotations
import logging
import time
import httpx
from ..config import REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT
from ..models import Workspace

log = logging.getLogger("collector.reddit")
_token_cache = {"token": None, "expires": 0}

def _get_token() -> str | None:
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        return None
    if _token_cache["token"] and _token_cache["expires"] > time.time():
        return _token_cache["token"]
    try:
        r = httpx.post("https://www.reddit.com/api/v1/access_token",
                       auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
                       data={"grant_type": "client_credentials"},
                       headers={"User-Agent": REDDIT_USER_AGENT}, timeout=20)
        r.raise_for_status()
        data = r.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires"] = time.time() + data.get("expires_in", 3600) - 60
        return _token_cache["token"]
    except Exception:  # noqa: BLE001
        log.exception("Reddit auth failed")
        return None

def collect(db, ws: Workspace) -> list[dict]:
    token = _get_token()
    if not token:
        log.info("Reddit not configured — skipping (set REDDIT_CLIENT_ID/SECRET)")
        return []
    try:
        r = httpx.get("https://oauth.reddit.com/search",
                      headers={"Authorization": f"bearer {token}", "User-Agent": REDDIT_USER_AGENT},
                      params={"q": ws.name, "sort": "new", "limit": 25}, timeout=20)
        r.raise_for_status()
        posts = r.json().get("data", {}).get("children", [])
    except Exception:  # noqa: BLE001
        log.exception("Reddit search failed for %s", ws.name)
        return []
    out = []
    for p in posts:
        d = p.get("data", {})
        text = (d.get("title", "") + ". " + d.get("selftext", "")[:400]).strip()
        if not text:
            continue
        out.append({"text": text, "url": "https://reddit.com" + d.get("permalink", ""),
                   "author": "u/" + d.get("author", "unknown"), "platform": "Forum",
                   "posted_at": None, "reach": d.get("score", 0)})
    return out
