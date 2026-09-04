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


def _matching_keywords(ws: Workspace, text: str) -> list[str]:
    """Returns every one of the workspace's own configured keywords (plus
    the brand name itself, as "<brand name>") that genuinely appears in
    this text — deliberately NOT brand_tokens; see the note below. An empty
    list means this mention doesn't survive the prefilter at all."""
    lower = text.lower()
    matched = []
    if ws.name.lower() in lower:
        matched.append(ws.name)
    for kw in (ws.keywords or []):
        if kw.lower() in lower:
            matched.append(kw)
    return matched


def _keyword_prefilter(ws: Workspace, text: str) -> bool:
    """Matches the full brand name, or one of the workspace's own explicitly
    configured keywords — deliberately NOT brand_tokens. Those are individual
    word fragments of the company name (e.g. "Kabod Global Resources" splits
    into "kabod", "global", "resources"), useful for spotting a look-alike
    domain containing one of them, but far too loose for filtering general
    content: "global" or "resources" alone would match huge numbers of
    completely unrelated articles. A mention should only survive here if it
    genuinely names the brand, or matches a keyword the user chose on purpose."""
    return bool(_matching_keywords(ws, text))


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
    survivors_with_keywords = [(c, _matching_keywords(ws, c["text"])) for c in candidates]
    survivors_with_keywords = [(c, kws) for c, kws in survivors_with_keywords if kws]
    survivors = [c for c, _ in survivors_with_keywords]
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
            matched_keywords=survivors_with_keywords[i][1],
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
