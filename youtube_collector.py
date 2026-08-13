"""YouTube — free Data API key from Google Cloud Console. Not a review process,
just enable the API and copy a key; works within minutes."""
from __future__ import annotations
import logging
import httpx
from ..config import YOUTUBE_API_KEY
from ..models import Workspace

log = logging.getLogger("collector.youtube")

def collect(db, ws: Workspace) -> list[dict]:
    if not YOUTUBE_API_KEY:
        log.info("YouTube not configured — skipping (set YOUTUBE_API_KEY)")
        return []
    query = ws.youtube_query or ws.name
    try:
        r = httpx.get("https://www.googleapis.com/youtube/v3/search", params={
            "part": "snippet", "q": query, "type": "video", "order": "date",
            "maxResults": 15, "key": YOUTUBE_API_KEY}, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception:  # noqa: BLE001
        log.exception("YouTube search failed for %s", query)
        return []
    out = []
    for it in items:
        sn = it.get("snippet", {})
        vid = it.get("id", {}).get("videoId", "")
        text = (sn.get("title", "") + ". " + sn.get("description", "")[:300]).strip()
        out.append({"text": text, "url": f"https://youtube.com/watch?v={vid}" if vid else "",
                   "author": sn.get("channelTitle", ""), "platform": "Video", "posted_at": None, "reach": 0})
    return out
