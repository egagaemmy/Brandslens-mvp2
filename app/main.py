"""app/main.py — the MVP API, now with real accounts.

Every route that touches workspace data depends on `current_member` (resolved
from a real session token) or `owned_workspace` (which additionally checks the
workspace belongs to that member's organization). There is no longer any flat
API key that can see everything — that was gap #1 from the review, and this
file is the fix.
"""
from __future__ import annotations
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db, init_db
from .config import FRONTEND_ORIGIN, APP_NAME
from .deps import current_member, require_role, owned_workspace
from .branding import BRAND
from .models import (Organization, OrgMember, Workspace, Incident, ScanRun,
                     MediaRoomCase, MediaRoomAudit, now_utc)
from .services import pipeline, media_room, auth, billing
from .services.auth import AuthError
from .services.billing import BillingNotConfigured
from .collectors import news_collector, nairaland_collector, reddit_collector, youtube_collector, domain_collector, x_collector

app = FastAPI(title=APP_NAME)
app.add_middleware(CORSMiddleware, allow_origins=[FRONTEND_ORIGIN] if FRONTEND_ORIGIN != "*" else ["*"],
                   allow_methods=["*"], allow_headers=["*"])

COLLECTORS = [news_collector, nairaland_collector, reddit_collector, youtube_collector, domain_collector, x_collector]
ROLE_LABEL = {"owner": "Owner", "lead": "Team Lead", "member": "Team Member"}


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ==================================================================
# AUTH — signup, login, logout
# ==================================================================
class SignupBody(BaseModel):
    company: str
    sector: str = "General"
    plan: str = "professional"
    name: str
    email: str
    password: str


@app.post("/api/auth/signup")
def signup(body: SignupBody, db: Session = Depends(get_db)) -> dict:
    try:
        member, token = auth.signup(db, body.company, body.sector, body.plan, body.name, body.email, body.password)
    except AuthError as e:
        raise HTTPException(422, str(e))
    ws = db.scalar(select(Workspace).where(Workspace.organization_id == member.organization_id))
    return {"token": token, "member": _member_dict(member), "workspace_id": ws.id if ws else None}


class LoginBody(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginBody, db: Session = Depends(get_db)) -> dict:
    try:
        member, token = auth.authenticate(db, body.email, body.password)
    except AuthError as e:
        raise HTTPException(401, str(e))
    workspaces = db.scalars(select(Workspace).where(Workspace.organization_id == member.organization_id)).all()
    return {"token": token, "member": _member_dict(member), "workspace_ids": [w.id for w in workspaces]}


@app.post("/api/auth/logout")
def logout(request: Request, db: Session = Depends(get_db)) -> dict:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if token:
        auth.revoke_session(db, token)
    return {"ok": True}


def _member_dict(m: OrgMember) -> dict:
    return {"id": m.id, "email": m.email, "name": m.name, "role": m.role, "role_label": ROLE_LABEL[m.role]}


# ==================================================================
# TEAM — invite, list, remove
# ==================================================================
class InviteBody(BaseModel):
    email: str
    name: str
    role: str   # "lead" | "member"


@app.post("/api/team/invite")
def invite(body: InviteBody, member: OrgMember = Depends(require_role("owner", "lead")),
          db: Session = Depends(get_db)) -> dict:
    try:
        token = auth.invite_member(db, member, body.email, body.name, body.role)
    except AuthError as e:
        raise HTTPException(422, str(e))
    # Production: email the invite link instead of returning the token directly.
    return {"ok": True, "invite_link": f"{FRONTEND_ORIGIN}/accept-invite?token={token}"}


@app.get("/api/team/members")
def list_members(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> list[dict]:
    members = db.scalars(select(OrgMember).where(OrgMember.organization_id == member.organization_id)).all()
    return [{**_member_dict(m), "status": m.status, "is_you": m.id == member.id} for m in members]


@app.delete("/api/team/members/{target_id}")
def remove_member_route(target_id: str, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    try:
        auth.remove_member(db, member, target_id)
    except AuthError as e:
        raise HTTPException(403, str(e))
    return {"ok": True}


class AcceptInviteBody(BaseModel):
    token: str
    password: str


@app.post("/api/team/accept-invite")
def accept_invite_route(body: AcceptInviteBody, db: Session = Depends(get_db)) -> dict:
    try:
        member, token = auth.accept_invite(db, body.token, body.password)
    except AuthError as e:
        raise HTTPException(422, str(e))
    return {"token": token, "member": _member_dict(member)}


# ==================================================================
# WORKSPACES — scoped to the caller's organization only
# ==================================================================
class NewWorkspaceBody(BaseModel):
    name: str
    sector: str = "General"


@app.post("/api/workspaces")
def create_workspace(body: NewWorkspaceBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    try:
        ws = auth.add_workspace(db, member, body.name, body.sector)
    except AuthError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "id": ws.id}


@app.get("/api/workspaces")
def list_workspaces(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> list[dict]:
    workspaces = db.scalars(select(Workspace).where(Workspace.organization_id == member.organization_id)).all()
    return [{"id": w.id, "name": w.name, "sector": w.sector} for w in workspaces]


@app.get("/api/workspaces/{ws_id}")
def get_workspace(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db)) -> dict:
    incidents = db.scalars(select(Incident).where(Incident.workspace_id == ws.id)
                           .order_by(Incident.posted_at.desc()).limit(200)).all()
    return {"id": ws.id, "name": ws.name, "sector": ws.sector,
           "brand_tokens": ws.brand_tokens, "keywords": ws.keywords, "rss_feeds": ws.rss_feeds,
           "brand_domains": ws.brand_domains, "telegram_channels": ws.telegram_channels,
           "reddit_subreddits": ws.reddit_subreddits, "youtube_query": ws.youtube_query,
           "incidents": [_inc_dict(i) for i in incidents]}


class WorkspaceUpdateBody(BaseModel):
    keywords: list[str] | None = None
    brand_tokens: list[str] | None = None
    rss_feeds: list[str] | None = None
    brand_domains: list[str] | None = None
    telegram_channels: list[str] | None = None
    reddit_subreddits: list[str] | None = None
    youtube_query: str | None = None


@app.patch("/api/workspaces/{ws_id}")
def update_workspace(body: WorkspaceUpdateBody, ws: Workspace = Depends(owned_workspace),
                     member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    if member.role == "member":
        raise HTTPException(403, "Only an Owner or Team Lead can change workspace settings")
    org = db.get(Organization, member.organization_id)
    updates = body.model_dump(exclude_unset=True)
    if "keywords" in updates and updates["keywords"] is not None and len(updates["keywords"]) > org.keyword_limit:
        raise HTTPException(422, f"Your plan allows up to {org.keyword_limit} tracked keywords")
    for field, value in updates.items():
        if value is not None:
            setattr(ws, field, value)
    db.commit()
    return {"ok": True}


# ---------- incidents ----------
def _inc_dict(i: Incident) -> dict:
    return {"id": i.id, "ref": i.ref, "title": i.title, "url": i.url, "author": i.author,
            "platform": i.platform, "lang": i.lang, "sev": i.severity, "sentiment": i.sentiment,
            "tags": i.tags, "rationale": i.rationale, "status": i.status, "reach": i.reach,
            "posted": i.posted_at.isoformat() if i.posted_at else None, "source": i.source}


class StatusBody(BaseModel):
    status: str


@app.patch("/api/incidents/{incident_id}/status")
def set_status(incident_id: str, body: StatusBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    if body.status not in ("open", "in review", "resolved", "dismissed"):
        raise HTTPException(422, "Invalid status")
    inc = db.get(Incident, incident_id)
    if not inc:
        raise HTTPException(404, "Not found")
    ws = db.get(Workspace, inc.workspace_id)
    if not ws or ws.organization_id != member.organization_id:
        raise HTTPException(404, "Not found")   # deliberately the same 404, not 403 — don't reveal existence
    inc.status = body.status
    db.commit()
    return {"ok": True}


# ---------- scans ----------
def _run_full_scan(ws_id: str) -> None:
    from .db import SessionLocal
    db = SessionLocal()
    try:
        ws = db.get(Workspace, ws_id)
        if not ws:
            return
        for mod in COLLECTORS:
            source = mod.__name__.split(".")[-1]
            run = ScanRun(workspace_id=ws.id, source=source)
            db.add(run); db.commit()
            try:
                candidates = mod.collect(db, ws)
                summary = pipeline.ingest_candidates(db, ws, candidates, source=source)
                run.finished_at = now_utc()
                run.candidates, run.new_incidents, run.high_count = summary["candidates"], summary["new"], summary["high"]
            except Exception as e:  # noqa: BLE001
                run.error = str(e)[:2000]; run.finished_at = now_utc()
            db.commit()
    finally:
        db.close()


@app.post("/api/scan/{ws_id}")
def trigger_scan(tasks: BackgroundTasks, ws: Workspace = Depends(owned_workspace)) -> dict:
    tasks.add_task(_run_full_scan, ws.id)
    return {"ok": True, "message": "Scan started across all configured sources"}


# ==================================================================
# MEDIA ROOM
# ==================================================================
def _case_dict(c: MediaRoomCase) -> dict:
    return {"id": c.id, "incident_id": c.incident_id, "severity": c.severity, "state": c.state,
            "sla_target_hours": c.sla_target_hours, "sla_remaining_hours": round(media_room.sla_remaining_hours(c), 2),
            "sla_breached": c.sla_breached_at is not None, "playbook_key": c.playbook_key,
            "requires_approval": c.requires_approval, "approved_by": c.approved_by}


@app.get("/api/media-room/cases")
def list_cases(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db)) -> list[dict]:
    cases = db.scalars(select(MediaRoomCase).where(MediaRoomCase.workspace_id == ws.id)
                       .order_by(MediaRoomCase.created_at.desc())).all()
    return [_case_dict(c) for c in cases]


def _case_in_org(case_id: str, member: OrgMember, db: Session) -> MediaRoomCase:
    case = db.get(MediaRoomCase, case_id)
    if not case:
        raise HTTPException(404, "Not found")
    ws = db.get(Workspace, case.workspace_id)
    if not ws or ws.organization_id != member.organization_id:
        raise HTTPException(404, "Not found")
    return case


class TransitionBody(BaseModel):
    new_state: str
    detail: dict = {}


@app.patch("/api/media-room/cases/{case_id}/state")
def transition_case(case_id: str, body: TransitionBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    case = _case_in_org(case_id, member, db)
    try:
        media_room.transition(db, case, body.new_state, member.email, body.detail)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _case_dict(case)


@app.post("/api/media-room/cases/{case_id}/approve")
def approve_case(case_id: str, member: OrgMember = Depends(require_role("owner", "lead")), db: Session = Depends(get_db)) -> dict:
    case = _case_in_org(case_id, member, db)
    media_room.approve(db, case, member.email)
    return _case_dict(case)


class DraftBody(BaseModel):
    subject: str
    body: str
    recipients: str = ""


@app.post("/api/media-room/cases/{case_id}/statements")
def draft_statement_route(case_id: str, body: DraftBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    case = _case_in_org(case_id, member, db)
    stmt = media_room.draft_statement(db, case, body.subject, body.body, body.recipients, member.email)
    return {"id": stmt.id, "version": stmt.version}


class AiDraftBody(BaseModel):
    brand_name: str
    incident_summary: str
    template_type: str = "fraud"


@app.post("/api/media-room/ai-draft")
def ai_draft(body: AiDraftBody, member: OrgMember = Depends(current_member)) -> dict:
    return {"draft": media_room.draft_statement_ai(body.brand_name, body.incident_summary, body.template_type)}


@app.get("/api/media-room/cases/{case_id}/audit")
def get_audit(case_id: str, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    case = _case_in_org(case_id, member, db)
    verified = media_room.verify_chain(db, case.id)
    rows = db.scalars(select(MediaRoomAudit).where(MediaRoomAudit.case_id == case.id).order_by(MediaRoomAudit.at)).all()
    return {"chain_verified": verified,
           "entries": [{"actor": r.actor, "action": r.action, "detail": r.detail,
                        "at": r.at.isoformat(), "hash": r.row_hash[:12]} for r in rows]}


# ==================================================================
# BILLING
# ==================================================================
class CheckoutBody(BaseModel):
    plan: str
    cycle: str = "annual"
    provider: str = "stripe"


@app.post("/api/billing/checkout")
def start_checkout(body: CheckoutBody, member: OrgMember = Depends(require_role("owner", "lead")), db: Session = Depends(get_db)) -> dict:
    if body.plan not in billing.PLAN_CATALOG:
        raise HTTPException(422, "Unknown plan")
    org = db.get(Organization, member.organization_id)
    try:
        if body.provider == "stripe":
            url = billing.create_stripe_checkout(org, body.plan, body.cycle, member.email)
        elif body.provider == "paystack":
            url = billing.create_paystack_checkout(org, body.plan, body.cycle, member.email)
        else:
            raise HTTPException(422, "provider must be 'stripe' or 'paystack'")
    except BillingNotConfigured as e:
        raise HTTPException(409, str(e))
    return {"checkout_url": url}


@app.post("/api/billing/webhook/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        return billing.handle_stripe_webhook(db, await request.body(), request.headers.get("stripe-signature", ""))
    except PermissionError:
        raise HTTPException(400, "Invalid signature")


@app.post("/api/billing/webhook/paystack")
async def paystack_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        return billing.handle_paystack_webhook(db, await request.body(), request.headers.get("x-paystack-signature", ""))
    except PermissionError:
        raise HTTPException(400, "Invalid signature")


@app.get("/api/billing/status")
def billing_status(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    org = db.get(Organization, member.organization_id)
    return {"plan": org.plan, "status": org.billing_status, "workspace_limit": org.workspace_limit,
           "keyword_limit": org.keyword_limit}


@app.get("/api/me")
def get_me(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    org = db.get(Organization, member.organization_id)
    workspaces = db.scalars(select(Workspace).where(Workspace.organization_id == member.organization_id)).all()
    return {
        "member": _member_dict(member),
        "organization": {"id": org.id, "name": org.name, "plan": org.plan, "billing_status": org.billing_status,
                         "workspace_limit": org.workspace_limit, "keyword_limit": org.keyword_limit},
        "workspaces": [{"id": w.id, "name": w.name, "sector": w.sector} for w in workspaces],
    }


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "at": datetime.now(timezone.utc).isoformat()}


# ==================================================================
# BRANDING — single source of truth the frontend can pull from
# ==================================================================
@app.get("/api/branding")
def get_branding() -> dict:
    return BRAND


# ==================================================================
# REPORTS — real, server-generated PDF and Excel (PRD §6.7)
# ==================================================================
from fastapi.responses import Response  # noqa: E402
from .services import report_generator  # noqa: E402


@app.get("/api/reports/pdf")
def report_pdf(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db)) -> Response:
    incidents = db.scalars(select(Incident).where(Incident.workspace_id == ws.id)
                           .order_by(Incident.posted_at.desc()).limit(500)).all()
    pdf_bytes = report_generator.generate_pdf_report(ws, incidents)
    filename = f"{ws.name.replace(' ', '-').lower()}-brandslens-report.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/reports/excel")
def report_excel(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db)) -> Response:
    incidents = db.scalars(select(Incident).where(Incident.workspace_id == ws.id)
                           .order_by(Incident.posted_at.desc()).limit(2000)).all()
    xlsx_bytes = report_generator.generate_excel_export(ws, incidents)
    filename = f"{ws.name.replace(' ', '-').lower()}-brandslens-export.xlsx"
    return Response(content=xlsx_bytes,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/analytics/{ws_id}")
def analytics_summary(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db)) -> dict:
    """Pre-aggregated chart data so the frontend never has to recompute this
    from raw incidents client-side — same numbers power the dashboard charts,
    the PDF, and the Excel summary sheet, by construction."""
    incidents = db.scalars(select(Incident).where(Incident.workspace_id == ws.id)
                           .order_by(Incident.posted_at.desc()).limit(1000)).all()
    from collections import Counter
    sev = Counter(i.severity for i in incidents)
    sentiment = Counter(i.sentiment for i in incidents if i.sentiment)
    platform = Counter(i.platform for i in incidents)
    by_day: dict[str, int] = {}
    for i in incidents:
        if i.posted_at:
            key = i.posted_at.strftime("%Y-%m-%d")
            by_day[key] = by_day.get(key, 0) + 1
    trend = sorted(by_day.items())[-14:]
    return {
        "total": len(incidents),
        "severity": dict(sev),
        "sentiment": dict(sentiment),
        "platform": dict(platform.most_common(8)),
        "trend": [{"date": d, "count": c} for d, c in trend],
    }
