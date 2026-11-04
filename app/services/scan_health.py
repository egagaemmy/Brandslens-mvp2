"""services/scan_health.py. Lets a customer see that BrandsLens is looking, and lets the team tell "nothing found" from "could not look".

Each source that really ran leaves ONE small heartbeat record per workspace (a row in the existing scan_runs table, updated in place, so the
table does not grow). A source that is not connected (no key, switched off) leaves nothing, so it is never shown or counted.
What the customer sees is only the sources that are being watched, and a gentle note when one was busy and will be retried.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ScanRun, now_utc, aware

log = logging.getLogger("scan_health")
LABELS = {"news": "News", "nairaland": "Nairaland", "hackernews": "Hacker News", "reddit": "Reddit", "youtube": "YouTube", "domains": "Domain watch", "x": "X"}
FRESH_FOR = timedelta(hours=36)             # the slowest source (domain watch) runs once a day
_state: ContextVar[dict | None] = ContextVar("scan_health", default=None)


def key_for(name: str) -> str:
    """'news_collector' -> 'news', 'domain_collector' -> 'domains': one key per source, however the caller names it."""
    key = name.replace("_collector", "")
    return "domains" if key == "domain" else key


class tracking:
    """Wrap one collector call. Collectors report a problem or a quiet skip through note_problem / note_skipped."""
    def __enter__(self) -> dict:
        self.state = {"problem": "", "skipped": False}
        self.token = _state.set(self.state)
        return self.state

    def __exit__(self, *exc) -> None:
        _state.reset(self.token)


def note_problem(reason: str) -> None:
    st = _state.get()
    if st is not None and not st["problem"]:
        st["problem"] = reason[:200]


def note_skipped() -> None:
    st = _state.get()
    if st is not None:
        st["skipped"] = True


def record(db: Session, ws, name: str, *, candidates: int = 0, new: int = 0, high: int = 0, problem: str = "") -> None:
    """Update this source's heartbeat for this workspace. Never lets a bookkeeping problem get in the way of scanning."""
    key = key_for(name)
    try:
        row = db.scalar(select(ScanRun).where(ScanRun.workspace_id == ws.id, ScanRun.source == key).order_by(ScanRun.started_at.desc()))
        if row is None:
            row = ScanRun(workspace_id=ws.id, source=key)
            db.add(row)
        row.started_at = row.finished_at = now_utc()
        row.candidates, row.new_incidents, row.high_count, row.error = candidates, new, high, (problem or "")[:2000]
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("Could not record the scan heartbeat for %s / %s", ws.id, key)


def status_for(db: Session, ws) -> dict:
    """{has_run, last_scan_at, watching: [labels], retrying: [labels]}: only sources that really ran recently."""
    cutoff = now_utc() - FRESH_FOR
    rows = db.scalars(select(ScanRun).where(ScanRun.workspace_id == ws.id, ScanRun.source.in_(list(LABELS)))).all()
    rows = [r for r in rows if r.finished_at and aware(r.finished_at) >= cutoff]
    order = list(LABELS)
    rows.sort(key=lambda r: order.index(r.source))
    last = max((aware(r.finished_at) for r in rows), default=None)
    return {"has_run": bool(rows), "last_scan_at": last.isoformat() if last else None,
            "watching": [LABELS[r.source] for r in rows], "retrying": [LABELS[r.source] for r in rows if r.error]}
