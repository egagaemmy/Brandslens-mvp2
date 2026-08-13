"""app/services/media_room.py — the escalation protocol.

State machine: detected -> under_review -> classified -> alerted ->
statement_drafted -> pending_approval (regulator track only) -> sent -> closed.

Every transition is hash-chained into an append-only audit trail: each row's
hash covers the previous row's hash, so tampering with history is provable at
read time (see verify_chain), not just a policy nobody checks.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import MediaRoomCase, MediaRoomAudit, MediaRoomStatement, Incident, aware

ALLOWED_TRANSITIONS = {
    "detected": {"under_review"},
    "under_review": {"classified"},
    "classified": {"alerted"},
    "alerted": {"statement_drafted"},
    "statement_drafted": {"pending_approval", "sent"},
    "pending_approval": {"sent", "under_review"},   # rejection sends it back for rework
    "sent": {"closed"},
}
SLA_TARGET_HOURS = {"HIGH": 4.0, "MEDIUM": 24.0, "WATCH": 72.0}

PLAYBOOKS = {
    "fraud":       {"title": "Fraud / Impersonation Alert", "requires_approval": False},
    "domain":      {"title": "Look-Alike Domain Takedown",  "requires_approval": False},
    "disclosure":  {"title": "Premature Disclosure Warning","requires_approval": False},
    "misinfo":     {"title": "Misinformation Correction",   "requires_approval": False},
    "regulator":   {"title": "Regulator Notification",      "requires_approval": True},
}


def _playbook_for(incident: Incident) -> str:
    tags = incident.tags or []
    if "DOMAIN RISK" in tags:
        return "domain"
    if "FRAUD" in tags:
        return "fraud"
    if "DISCLOSURE RISK" in tags:
        return "disclosure"
    return "misinfo"


def open_case(db: Session, incident: Incident) -> MediaRoomCase:
    playbook = _playbook_for(incident)
    case = MediaRoomCase(
        workspace_id=incident.workspace_id, incident_id=incident.id, severity=incident.severity,
        sla_target_hours=SLA_TARGET_HOURS.get(incident.severity, 24.0),
        playbook_key=playbook, requires_approval=PLAYBOOKS[playbook]["requires_approval"],
    )
    db.add(case)
    db.flush()
    _audit(db, case, actor="system", action="case_opened",
          detail={"incident_ref": incident.ref, "severity": incident.severity, "playbook": playbook})
    db.commit()
    return case


def transition(db: Session, case: MediaRoomCase, new_state: str, actor: str, detail: dict | None = None) -> MediaRoomCase:
    if new_state not in ALLOWED_TRANSITIONS.get(case.state, set()):
        raise ValueError(f"Illegal transition {case.state} -> {new_state}")
    if new_state == "sent" and case.requires_approval and not case.approved_by:
        raise ValueError("This case requires written approval before it can be sent")
    old = case.state
    case.state = new_state
    case.updated_at = datetime.now(timezone.utc)
    _audit(db, case, actor=actor, action="state_change", detail={"from": old, "to": new_state, **(detail or {})})
    db.commit()
    return case


def approve(db: Session, case: MediaRoomCase, actor: str) -> MediaRoomCase:
    case.approved_by = actor
    _audit(db, case, actor=actor, action="approved", detail={})
    db.commit()
    return case


def draft_statement(db: Session, case: MediaRoomCase, subject: str, body: str,
                    recipients: str, drafted_by: str = "ai") -> MediaRoomStatement:
    version = (db.scalar(select(MediaRoomStatement).where(MediaRoomStatement.case_id == case.id)
                        .order_by(MediaRoomStatement.version.desc())) or type("x", (), {"version": 0})).version + 1
    stmt = MediaRoomStatement(case_id=case.id, version=version, drafted_by=drafted_by,
                             subject=subject, body=body, recipients=recipients)
    db.add(stmt)
    _audit(db, case, actor=drafted_by, action="statement_drafted", detail={"version": version, "subject": subject})
    db.commit()
    return stmt


def sla_remaining_hours(case: MediaRoomCase) -> float:
    elapsed = (datetime.now(timezone.utc) - aware(case.sla_started_at)).total_seconds() / 3600
    return case.sla_target_hours - elapsed


def sweep_sla_breaches(db: Session) -> list[MediaRoomCase]:
    """Run on a schedule (see worker.py). Marks lapsed cases so the caller can
    fire a second-tier alert — a HIGH case with no action for 4 hours is itself
    a signal something is wrong with the process, not just the underlying threat."""
    open_cases = db.scalars(select(MediaRoomCase).where(MediaRoomCase.state.notin_(["sent", "closed"]))).all()
    breached = []
    for case in open_cases:
        if sla_remaining_hours(case) <= 0 and not case.sla_breached_at:
            case.sla_breached_at = datetime.now(timezone.utc)
            _audit(db, case, actor="system", action="sla_breached", detail={"target_hours": case.sla_target_hours})
            breached.append(case)
    db.commit()
    return breached


def _audit(db: Session, case: MediaRoomCase, actor: str, action: str, detail: dict) -> MediaRoomAudit:
    last = db.scalars(select(MediaRoomAudit).where(MediaRoomAudit.case_id == case.id)
                      .order_by(MediaRoomAudit.at.desc()).limit(1)).first()
    prev_hash = last.row_hash if last else "genesis"
    at = datetime.now(timezone.utc)
    payload = json.dumps({"actor": actor, "action": action, "detail": detail, "at": at.isoformat()}, sort_keys=True)
    row_hash = hashlib.sha256((prev_hash + payload).encode()).hexdigest()
    entry = MediaRoomAudit(case_id=case.id, actor=actor, action=action, detail=detail,
                          prev_hash=prev_hash, row_hash=row_hash, at=at)
    db.add(entry)
    return entry


def verify_chain(db: Session, case_id: str) -> bool:
    """Recomputes every hash in a case's trail. A tampered row fails here —
    call this before ever returning an audit trail to a client."""
    rows = db.scalars(select(MediaRoomAudit).where(MediaRoomAudit.case_id == case_id)
                      .order_by(MediaRoomAudit.at)).all()
    prev = "genesis"
    for row in rows:
        payload = json.dumps({"actor": row.actor, "action": row.action, "detail": row.detail,
                              "at": row.at.isoformat()}, sort_keys=True)
        expected = hashlib.sha256((prev + payload).encode()).hexdigest()
        if expected != row.row_hash or row.prev_hash != prev:
            return False
        prev = row.row_hash
    return True


def draft_statement_ai(brand_name: str, incident_summary: str, template_type: str) -> str:
    """Optional AI-assisted draft via Claude. Deliberately constrained: drafts
    only, never claims resolution or fault, always flags for human review."""
    from ..config import ANTHROPIC_API_KEY, CLASSIFIER_MODEL
    if not ANTHROPIC_API_KEY:
        return (f"[Draft — Claude not configured, write manually]\n\n"
                f"Regarding: {incident_summary}\n\nRecommended position: acknowledge awareness, "
                f"avoid confirming unverified details, commit to a follow-up within 24 hours.")
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    system = ("You draft crisis and regulatory communications. Draft ONLY — never state a matter "
             "is resolved, never admit fault, never make legal claims. Always leave a placeholder "
             "for legal/compliance review. Be factual and calm, no speculation about unverified details.")
    resp = client.messages.create(
        model=CLASSIFIER_MODEL, max_tokens=500, system=system,
        messages=[{"role": "user", "content":
            f"Brand: {brand_name}\nTemplate: {template_type}\nIncident: {incident_summary}\n\nDraft the statement."}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")
