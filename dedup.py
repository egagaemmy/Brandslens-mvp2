"""Near-duplicate folding — cheap, dependency-free token-overlap comparison.
Good enough at MVP volume; swap for embedding similarity once volume justifies it."""
from __future__ import annotations
import hashlib
import re
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import Incident

_WORD = re.compile(r"[a-z0-9\u00c0-\u024f]+")

def content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(_WORD.findall(text.lower())).encode()).hexdigest()

def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))

def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def find_near_duplicate(db: Session, workspace_id: str, text: str,
                        window_hours: int = 72, threshold: float = 0.82) -> Incident | None:
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    cand = _tokens(text)
    recent = db.scalars(
        select(Incident).where(Incident.workspace_id == workspace_id, Incident.logged_at >= since)
        .order_by(Incident.logged_at.desc()).limit(300)
    ).all()
    for inc in recent:
        if jaccard(cand, _tokens(inc.title)) >= threshold:
            return inc
    return None
