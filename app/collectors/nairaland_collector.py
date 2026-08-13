"""Nairaland — no API exists at all, so respectful scraping is the only route.
Slow cadence, identifying User-Agent, honours robots.txt in spirit."""
from __future__ import annotations
import logging
import re
import httpx
from ..models import Workspace

log = logging.getLogger("collector.nairaland")
UA = {"User-Agent": "BrandsLensMVP/0.1 (brand monitoring; contact watch@brandseye.app)"}
LINK_RE = re.compile(r'<a href="(https://www\.nairaland\.com/\d+/[^"]+)"[^>]*>(.*?)</a>', re.I)
TAG_RE = re.compile(r"<[^>]+>")

def collect(db, ws: Workspace) -> list[dict]:
    q = ws.name.replace(" ", "+")
    try:
        r = httpx.get(f"https://www.nairaland.com/search/{q}/0/0/0/0", headers=UA, timeout=30)
        r.raise_for_status()
    except Exception:  # noqa: BLE001
        log.exception("Nairaland fetch failed for %s", ws.name)
        return []
    out, seen = [], set()
    for url, raw_title in LINK_RE.findall(r.text)[:20]:
        title = TAG_RE.sub("", raw_title).strip()
        if not title or url in seen:
            continue
        seen.add(url)
        out.append({"text": title, "url": url, "author": "nairaland", "platform": "Forum", "posted_at": None, "reach": 0})
    return out
