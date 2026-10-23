"""services/reclassify.py. Re-score the mentions that were stored while the AI was unavailable.

When the AI key had no credit, every new mention was saved as WATCH with the rationale below, instead of being
dropped. This finds exactly those mentions (nothing a person has changed, nothing the AI already scored) and
runs them through the classifier again, in small batches, stopping at the first sign that the AI is still
unavailable so it can never burn credit on a loop.

It is a DRY RUN by default: it reports what it would change and changes nothing until you ask. When it applies a
change it updates the mention and, for HIGH and MEDIUM, opens the Media Room case the original pipeline would have
opened. It never sends alerts or emails for old mentions."""
from __future__ import annotations
import logging
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Incident, MediaRoomCase, Organization, Workspace
from . import classifier, media_room

log = logging.getLogger("reclassify")
FALLBACK_MARK = "Fallback — classifier unavailable or errored."


def find_failed(db: Session, limit: int = 200) -> list[Incident]:
    return list(db.scalars(
        select(Incident).where(Incident.rationale == FALLBACK_MARK, Incident.severity_overridden.isnot(True))
        .order_by(Incident.posted_at.desc()).limit(limit)).all())


def run(db: Session, dry_run: bool = True, limit: int = 200, batch_size: int = 15) -> dict:
    todo = find_failed(db, limit)
    by_ws: dict[str, list[Incident]] = {}
    for inc in todo:
        by_ws.setdefault(inc.workspace_id, []).append(inc)
    outcome: Counter = Counter()
    stopped = ""
    examined = 0
    for ws_id, incidents in by_ws.items():
        ws = db.get(Workspace, ws_id)
        org = db.get(Organization, ws.organization_id) if ws else None
        if not ws or (org and org.is_demo):
            continue
        for start in range(0, len(incidents), batch_size):
            chunk = incidents[start:start + batch_size]
            items = [{"idx": i, "text": inc.title, "platform": inc.platform} for i, inc in enumerate(chunk)]
            results = classifier.classify_batch(ws.name, f"Sector: {ws.sector}.", items)
            by_idx = {int(r["idx"]): r for r in results if str(r.get("idx", "")).lstrip("-").isdigit()}
            if all((by_idx.get(i) or {}).get("rationale") == FALLBACK_MARK for i in range(len(chunk))):
                stopped = "The AI did not answer (no credit or a key problem). Nothing further was attempted. Run /api/setup/ai-check."
                break
            for i, inc in enumerate(chunk):
                r = by_idx.get(i)
                examined += 1
                if not r or r.get("rationale") == FALLBACK_MARK:
                    outcome["still_unscored"] += 1
                    continue
                outcome[r["severity"]] += 1
                if dry_run:
                    continue
                inc.severity, inc.sentiment, inc.tags = r["severity"], r["sentiment"], r["tags"]
                inc.lang, inc.rationale = r["lang"], r["rationale"]
                if r["severity"] in ("HIGH", "MEDIUM") and not db.scalar(select(MediaRoomCase.id).where(MediaRoomCase.incident_id == inc.id)):
                    media_room.open_case(db, inc)
            if not dry_run:
                db.commit()
        if stopped:
            break
    return {"dry_run": dry_run, "found": len(todo), "examined": examined, "would_become" if dry_run else "became": dict(outcome),
            "stopped": stopped or None,
            "next": ("Run again with dry_run=false to apply." if dry_run and not stopped and examined else None)}
