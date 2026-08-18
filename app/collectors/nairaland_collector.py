"""Nairaland — no API exists at all, so respectful scraping is the only route.
Slow cadence, identifying User-Agent, honours robots.txt in spirit."""
from __future__ import annotations
import logging
import re
import httpx
from ..models import Workspace
from ..services.pipeline import search_terms

log = logging.getLogger("collector.nairaland")
UA = {"User-Agent": "BrandsLensMVP/0.1 (brand monitoring; contact watch@brandseye.app)"}
LINK_RE = re.compile(r'<a href="(https://www\.nairaland\.com/\d+/[^"]+)"[^>]*>(.*?)</a>', re.I)
TAG_RE = re.compile(r"<[^>]+>")

def collect(db, ws: Workspace) -> list[dict]:
    # Nairaland's search has no boolean OR support (it's a plain URL path per
    # term), so — unlike GDELT/Reddit — we make one request per term rather
    # than combining them. Capped at 3 terms per run to stay genuinely
    # respectful of a site with no official API, per the module's own policy.
    out, seen = [], set()
    for term in search_terms(ws, limit=3):
        out.extend(_search_one(term, seen))
    return out

def _search_one(term: str, seen: set[str]) -> list[dict]:
    q = term.replace(" ", "+")
    try:
        r = httpx.get(f"https://www.nairaland.com/search/{q}/0/0/0/0", headers=UA, timeout=30)
        r.raise_for_status()
    except Exception:  # noqa: BLE001
        log.exception("Nairaland fetch failed for %s", term)
        return []
    out = []
    for url, raw_title in LINK_RE.findall(r.text)[:20]:
        title = TAG_RE.sub("", raw_title).strip()
        if not title or url in seen:
            continue
        seen.add(url)
        out.append({"text": title, "url": url, "author": "nairaland", "platform": "Forum", "posted_at": None, "reach": 0})
    return out
