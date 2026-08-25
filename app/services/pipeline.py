"""app/services/pipeline.py — every collector funnels through this one path.
Keyword prefilter (free) -> Claude classify (cheap) -> dedup -> persist ->
auto-open a Media Room case on HIGH. This is the seam that makes adding a new
source later (X, Facebook, whatever) trivial: write a collector that returns
the same candidate shape, call ingest_candidates(), done.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from ..models import Workspace, Incident
from .classifier import classify_batch
from .dedup import content_hash, find_near_duplicate
from . import media_room
from .mailer import slack_alert

log = logging.getLogger("pipeline")


def _keyword_prefilter(ws: Workspace, text: str) -> bool:
    lower = text.lower()
    if ws.name.lower() in lower:
        return True
    return any(t.lower() in lower for t in (ws.brand_tokens or []) + (ws.keywords or []))


def search_terms(ws: Workspace, limit: int = 12) -> list[str]:
    """The brand name plus every tracked keyword — campaign names, product
    names, anything unrelated to the brand itself that's been added in
    Settings. Every search-based collector (news, Reddit, Nairaland, YouTube)
    reads from this single shared list, so adding a keyword once in Settings
    makes every source search for it, not just filter for it afterward."""
    terms = [ws.name] + list(ws.keywords or [])
    seen, out = set(), []
    for t in terms:
        key = t.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(t.strip())
    return out[:limit]


def _next_ref(db: Session, ws: Workspace) -> str:
    n = db.scalar(select(func.count(Incident.id)).where(Incident.workspace_id == ws.id)) or 0
    prefix = "".join(c for c in ws.name.upper() if c.isalpha())[:4] or "WS"
    return f"{prefix}-{1000 + n + 1}"


def ingest_candidates(db: Session, ws: Workspace, candidates: list[dict], source: str, found_historically: bool = False) -> dict:
    """candidates: [{"text","url","author","platform","posted_at"(datetime|None),"reach"}]"""
    survivors = [c for c in candidates if _keyword_prefilter(ws, c["text"])]
    if not survivors:
        return {"candidates": len(candidates), "new": 0, "high": 0, "merged": 0}

    classified = classify_batch(
        ws.name, f"Sector: {ws.sector}.",
        [{"idx": i, "text": c["text"], "platform": c.get("platform", "")} for i, c in enumerate(survivors)],
    )
    by_idx = {}
    for c in classified:
        try:
            by_idx[int(c["idx"])] = c
        except (TypeError, ValueError, KeyError):
            continue

    new = high = merged = 0
    for i, cand in enumerate(survivors):
        cls = by_idx.get(i) or {}
        text = cand["text"].strip()
        chash = content_hash(text)

        existing = db.scalar(select(Incident).where(
            Incident.workspace_id == ws.id, Incident.content_hash == chash))
        if existing is None:
            existing = find_near_duplicate(db, ws.id, text)
        if existing is not None:
            merged += 1
            continue

        inc = Incident(
            workspace_id=ws.id, ref=_next_ref(db, ws), title=text[:2000],
            url=cand.get("url", ""), author=cand.get("author", ""),
            platform=cand.get("platform", "News"), lang=cls.get("lang", "en"),
            severity=cls.get("severity", "WATCH"), sentiment=cls.get("sentiment", ""),
            tags=cls.get("tags", []), rationale=cls.get("rationale", ""),
            reach=int(cand.get("reach") or 0), content_hash=chash, source=source,
            posted_at=cand.get("posted_at") or datetime.now(timezone.utc),
            found_historically=found_historically,
        )
        db.add(inc)
        db.flush()
        new += 1

        if inc.severity == "HIGH":
            high += 1
            case = media_room.open_case(db, inc)
            slack_alert(f":rotating_light: HIGH — {ws.name}\n{inc.ref} · {inc.platform} · {inc.title[:200]}\n{inc.url}")
            log.info("Opened Media Room case %s for %s", case.id, inc.ref)
        elif inc.severity == "MEDIUM":
            case = media_room.open_case(db, inc)
            slack_alert(f":warning: MEDIUM — {ws.name}\n{inc.ref} · {inc.platform} · {inc.title[:200]}\n{inc.url}")
            log.info("Opened Media Room case %s for %s", case.id, inc.ref)

    db.commit()
    return {"candidates": len(candidates), "new": new, "high": high, "merged": merged}
