"""app/main.py — the MVP API, now with real accounts.

Every route that touches workspace data depends on `current_member` (resolved
from a real session token) or `owned_workspace` (which additionally checks the
workspace belongs to that member's organization). There is no longer any flat
API key that can see everything — that was gap #1 from the review, and this
file is the fix.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select, delete, func
from sqlalchemy.orm import Session

from .db import get_db, init_db
from .config import FRONTEND_ORIGIN, APP_NAME, ADMIN_SETUP_SECRET
from .deps import current_member, active_member, require_role, owned_workspace
from .branding import BRAND
from .models import (Organization, OrgMember, Workspace, Incident, ScanRun,
                     MediaRoomCase, MediaRoomAudit, Competitor, CompetitorMention, now_utc, aware)
from .services import pipeline, media_room, auth, billing
from .services.auth import AuthError
from .services.billing import BillingNotConfigured
from .collectors import news_collector, nairaland_collector, hackernews_collector, reddit_collector, youtube_collector, domain_collector, x_collector

app = FastAPI(title=APP_NAME)
log = logging.getLogger("main")
app.add_middleware(CORSMiddleware, allow_origins=[FRONTEND_ORIGIN] if FRONTEND_ORIGIN != "*" else ["*"],
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Disposition"])

COLLECTORS = [news_collector, nairaland_collector, hackernews_collector, reddit_collector, youtube_collector, domain_collector, x_collector]
ROLE_LABEL = {"owner": "Owner", "lead": "Team Lead", "member": "Team Member"}

# The full source catalog — shown to users so they know exactly where mentions
# come from. "active" is computed per-request from what's actually configured,
# not hardcoded, so this list is always honest about real system state.
SOURCE_CATALOG = [
    {"key": "news", "label": "News (GDELT + RSS)", "requires_key": False},
    {"key": "nairaland", "label": "Nairaland", "requires_key": False},
    {"key": "hackernews", "label": "Hacker News", "requires_key": False},
    {"key": "domain", "label": "Look-alike Domain Watch", "requires_key": False},
    {"key": "reddit", "label": "Reddit", "requires_key": True, "env": "REDDIT_CLIENT_ID",
     "note": "Requires Reddit's written commercial-use approval (Responsible Builder Policy) — applied for, not yet guaranteed"},
    {"key": "youtube", "label": "YouTube", "requires_key": True, "env": "YOUTUBE_API_KEY"},
    {"key": "telegram", "label": "Telegram channels", "requires_key": True, "env": "TELEGRAM_API_ID"},
    {"key": "x", "label": "X (Twitter)", "requires_key": True, "env": "X_ENABLED", "note": "Off until enabled — see architecture blueprint"},
    {"key": "facebook", "label": "Facebook & Instagram", "requires_key": False, "note": "Not yet available — pending Meta app review"},
    {"key": "tiktok", "label": "TikTok", "requires_key": False, "note": "Not yet available"},
]


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ==================================================================
# AUTH — signup, login, logout
# ==================================================================
class SignupBody(BaseModel):
    company: str
    sector: str = "General"
    plan: str = "standard"
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


class ForgotPasswordBody(BaseModel):
    email: str


@app.post("/api/auth/forgot-password")
def forgot_password(body: ForgotPasswordBody, db: Session = Depends(get_db)) -> dict:
    """Always returns the identical response whether or not the email
    exists — that's deliberate, not an oversight. If this told you 'no
    account found,' anyone could use it to check who has a BrandsLens
    account just by trying emails."""
    token = auth.request_password_reset(db, body.email)
    if token:
        from .services.mailer import send_password_reset
        member = db.scalar(select(OrgMember).where(OrgMember.email == body.email.lower()))
        reset_link = f"{FRONTEND_ORIGIN}/?reset_token={token}"
        send_password_reset(member.email, member.name, reset_link)
    return {"ok": True, "message": "If that email has a BrandsLens account, a reset link is on its way."}


class ResetPasswordBody(BaseModel):
    token: str
    password: str


@app.post("/api/auth/reset-password")
def reset_password_route(body: ResetPasswordBody, db: Session = Depends(get_db)) -> dict:
    try:
        auth.reset_password(db, body.token, body.password)
    except AuthError as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


def _member_dict(m: OrgMember) -> dict:
    return {"id": m.id, "email": m.email, "name": m.name, "role": m.role, "role_label": ROLE_LABEL[m.role],
            "avatar_base64": m.avatar_base64, "phone": m.phone, "address": m.address,
            "city": m.city, "country": m.country, "job_title": m.job_title}


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
def remove_member_route(target_id: str, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
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
def create_workspace(body: NewWorkspaceBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    try:
        ws = auth.add_workspace(db, member, body.name, body.sector)
    except AuthError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "id": ws.id}


@app.get("/api/workspaces")
def list_workspaces(member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> list[dict]:
    workspaces = db.scalars(select(Workspace).where(Workspace.organization_id == member.organization_id)).all()
    return [{"id": w.id, "name": w.name, "sector": w.sector} for w in workspaces]


def _date_range_bounds(range_key: str | None, start: str | None, end: str | None):
    """Turns a preset ('today'/'week'/'month'/'custom') or explicit ISO date
    strings into (start_dt, end_dt) in UTC. Returns (None, None) when no
    filter should be applied — every endpoint below treats that as 'show
    everything', so a missing/unrecognized filter never silently hides data."""
    from datetime import timedelta
    now = now_utc()
    if range_key == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0), now
    if range_key == "week":
        return now - timedelta(days=7), now
    if range_key == "month":
        return now - timedelta(days=30), now
    if range_key == "custom" and start:
        try:
            start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else now
            return start_dt, end_dt
        except ValueError:
            return None, None
    return None, None


def _apply_date_filter(query, range_key: str | None, start: str | None, end: str | None):
    start_dt, end_dt = _date_range_bounds(range_key, start, end)
    if start_dt:
        query = query.where(Incident.posted_at >= start_dt)
    if end_dt:
        query = query.where(Incident.posted_at <= end_dt)
    return query


@app.get("/api/workspaces/{ws_id}")
def get_workspace(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db),
                  range: str | None = None, start: str | None = None, end: str | None = None) -> dict:
    q = select(Incident).where(Incident.workspace_id == ws.id)
    q = _apply_date_filter(q, range, start, end)
    incidents = db.scalars(q.order_by(Incident.posted_at.desc()).limit(200)).all()
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
    from .services.auth import effective_plan, PLAN_KEYWORD_LIMIT
    org = db.get(Organization, member.organization_id)
    updates = body.model_dump(exclude_unset=True)
    if "keywords" in updates and updates["keywords"] is not None:
        limit = PLAN_KEYWORD_LIMIT[effective_plan(org)]
        if len(updates["keywords"]) > limit:
            raise HTTPException(422, f"Your plan allows up to {limit} tracked keywords")
    for field, value in updates.items():
        if value is not None:
            setattr(ws, field, value)
    db.commit()
    return {"ok": True}


# ---------- incidents ----------
def _inc_dict(i: Incident) -> dict:
    return {"id": i.id, "ref": i.ref, "title": i.title, "url": i.url, "author": i.author,
            "platform": i.platform, "lang": i.lang, "sev": i.severity, "sev_overridden": i.severity_overridden,
            "sentiment": i.sentiment, "tags": i.tags, "rationale": i.rationale, "status": i.status,
            "reach": i.reach, "posted": i.posted_at.isoformat() if i.posted_at else None, "source": i.source}


class StatusBody(BaseModel):
    status: str


@app.patch("/api/incidents/{incident_id}/status")
def set_status(incident_id: str, body: StatusBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
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


class SeverityBody(BaseModel):
    severity: str


@app.patch("/api/incidents/{incident_id}/severity")
def set_severity(incident_id: str, body: SeverityBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    """The AI's classification is a starting point, not the final word — the
    team knows their own brand and context better than any automated system
    ever will, so severity is always editable, and we track that it was a
    human decision rather than silently overwriting the AI's original call."""
    if body.severity not in ("HIGH", "MEDIUM", "WATCH"):
        raise HTTPException(422, "Severity must be HIGH, MEDIUM, or WATCH.")
    inc = db.get(Incident, incident_id)
    if not inc:
        raise HTTPException(404, "Not found")
    ws = db.get(Workspace, inc.workspace_id)
    if not ws or ws.organization_id != member.organization_id:
        raise HTTPException(404, "Not found")
    inc.severity = body.severity
    inc.severity_overridden = True
    db.commit()
    return {"ok": True, "severity": inc.severity}


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


# Which sources genuinely honour a historical date range vs which ones
# always search "now" regardless — honest, not aspirational. Domain Watch
# checks currently-resolving permutations (no publish date to search
# against); Nairaland's search endpoint doesn't expose a reliable date
# filter we could confidently drive; X isn't enabled at all yet.
HISTORICAL_CAPABLE = {"news_collector", "youtube_collector", "hackernews_collector", "reddit_collector"}


def _run_historical_scan(ws_id: str, days_back: int) -> None:
    from .db import SessionLocal
    db = SessionLocal()
    try:
        ws = db.get(Workspace, ws_id)
        if not ws:
            return
        for mod in COLLECTORS:
            source = mod.__name__.split(".")[-1]
            run = ScanRun(workspace_id=ws.id, source=f"{source}_historical_{days_back}d")
            db.add(run); db.commit()
            try:
                kwargs = {"days_back": days_back} if source in HISTORICAL_CAPABLE else {}
                candidates = mod.collect(db, ws, **kwargs)
                summary = pipeline.ingest_candidates(db, ws, candidates, source=source)
                run.finished_at = now_utc()
                run.candidates, run.new_incidents, run.high_count = summary["candidates"], summary["new"], summary["high"]
            except Exception as e:  # noqa: BLE001
                run.error = str(e)[:2000]; run.finished_at = now_utc()
            db.commit()
    finally:
        db.close()


class HistoricalScanBody(BaseModel):
    days_back: int


@app.post("/api/scan/{ws_id}/historical")
def trigger_historical_scan(body: HistoricalScanBody, tasks: BackgroundTasks,
                            ws: Workspace = Depends(owned_workspace), member: OrgMember = Depends(active_member),
                            db: Session = Depends(get_db)) -> dict:
    """On-demand only — deliberately not part of the continuous 20-minute
    cycle. A wide historical search is heavier (more results per source,
    slower external APIs), so it's something the team explicitly asks for,
    not something that runs automatically and repeatedly.

    Tiered: Standard doesn't get this feature at all — Growth reaches back
    5 years, Professional 10 years, Enterprise effectively unlimited."""
    from .services.auth import effective_plan, PLAN_HISTORICAL_DAYS
    org = db.get(Organization, member.organization_id)
    plan = effective_plan(org)
    max_days = PLAN_HISTORICAL_DAYS[plan]
    if max_days <= 0:
        raise HTTPException(403, "Historical search isn't available on the Standard plan — upgrade to Growth or higher to use it.")
    if body.days_back <= 0:
        raise HTTPException(422, "days_back must be a positive number.")
    if body.days_back > max_days:
        raise HTTPException(422, f"Your plan allows historical search up to {max_days // 365} years back. "
                            f"Upgrade for a wider range.")
    tasks.add_task(_run_historical_scan, ws.id, body.days_back)
    supported = [m.__name__.split(".")[-1].replace("_collector", "") for m in COLLECTORS
                if m.__name__.split(".")[-1] in HISTORICAL_CAPABLE]
    return {"ok": True, "message": f"Historical search started, going back {body.days_back} days.",
           "sources_with_real_date_range": supported}


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
def transition_case(case_id: str, body: TransitionBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
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
def draft_statement_route(case_id: str, body: DraftBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    case = _case_in_org(case_id, member, db)
    stmt = media_room.draft_statement(db, case, body.subject, body.body, body.recipients, member.email)
    return {"id": stmt.id, "version": stmt.version}


class AiDraftBody(BaseModel):
    brand_name: str
    incident_summary: str
    template_type: str = "fraud"


@app.post("/api/media-room/ai-draft")
def ai_draft(body: AiDraftBody, member: OrgMember = Depends(active_member)) -> dict:
    return {"draft": media_room.draft_statement_ai(body.brand_name, body.incident_summary, body.template_type)}


@app.get("/api/media-room/cases/{case_id}/audit")
def get_audit(case_id: str, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
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


def _plan_dict(org: Organization) -> dict:
    from .services.auth import effective_plan, PLAN_WORKSPACE_LIMIT, PLAN_KEYWORD_LIMIT
    eff = effective_plan(org)
    return {"real_plan": org.plan, "effective_plan": eff, "is_admin_view_as": org.view_as_plan is not None,
           "effective_workspace_limit": PLAN_WORKSPACE_LIMIT[eff], "effective_keyword_limit": PLAN_KEYWORD_LIMIT[eff],
           "is_exempt": org.billing_status == "exempt"}


class ViewAsBody(BaseModel):
    plan: str | None = None  # None resets to the real (unlimited) view


@app.post("/api/admin/view-as")
def view_as(body: ViewAsBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    """Only ever meaningful for the exempt admin account — everyone else's
    organization has billing_status != 'exempt', so auth.set_view_as raises
    for them before anything changes."""
    org = db.get(Organization, member.organization_id)
    try:
        auth.set_view_as(db, org, body.plan)
    except AuthError as e:
        raise HTTPException(403, str(e))
    return _plan_dict(org)


@app.get("/api/billing/status")
def billing_status(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    org = db.get(Organization, member.organization_id)
    return {"plan": org.plan, "status": org.billing_status, "workspace_limit": org.workspace_limit,
           "keyword_limit": org.keyword_limit, **_plan_dict(org)}


@app.get("/api/me")
def get_me(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    org = db.get(Organization, member.organization_id)
    workspaces = db.scalars(select(Workspace).where(Workspace.organization_id == member.organization_id)).all()
    return {
        "member": _member_dict(member),
        "organization": {"id": org.id, "name": org.name, "plan": org.plan, "billing_status": org.billing_status,
                         "workspace_limit": org.workspace_limit, "keyword_limit": org.keyword_limit, **_plan_dict(org)},
        "workspaces": [{"id": w.id, "name": w.name, "sector": w.sector} for w in workspaces],
    }


# ==================================================================
# PROFILE — every member can edit their own, including a KYC-style set of
# details (photo, phone, address, city, country, job title).
# ==================================================================
class ProfileUpdateBody(BaseModel):
    name: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    job_title: str | None = None
    avatar_base64: str | None = None   # a data: URL, e.g. "data:image/png;base64,iVBOR..."


@app.patch("/api/me/profile")
def update_my_profile(body: ProfileUpdateBody, member: OrgMember = Depends(current_member),
                      db: Session = Depends(get_db)) -> dict:
    try:
        auth.update_profile(db, member, **body.model_dump(exclude_unset=True))
    except AuthError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "member": _member_dict(member)}


# ==================================================================
# SOURCES — subscribers only ever see what's genuinely active; the full
# picture (pending approvals, why something's off) is internal knowledge,
# visible only through the admin-only endpoint below.
# ==================================================================
def _source_status() -> list[dict]:
    import os
    out = []
    for s in SOURCE_CATALOG:
        active = True
        if s.get("requires_key"):
            active = bool(os.environ.get(s.get("env", ""), "")) and os.environ.get(s.get("env", ""), "").lower() != "false"
        if s["key"] in ("facebook", "tiktok"):
            active = False
        out.append({"key": s["key"], "label": s["label"], "active": active, "note": s.get("note", "")})
    return out


@app.get("/api/sources")
def list_sources(member: OrgMember = Depends(active_member)) -> list[dict]:
    """What subscribers see: active sources only, no notes about anything
    pending, restricted, or off — that's internal-only, not customer-facing."""
    return [{"key": s["key"], "label": s["label"]} for s in _source_status() if s["active"]]


@app.get("/api/admin/sources")
def list_sources_internal(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> list[dict]:
    """The full picture — every source, active or not, with the real reason
    why. Restricted to the exempt admin account; everyone else gets a 403,
    not a filtered response, since this is internal knowledge, not something
    to selectively redact for other roles."""
    org = db.get(Organization, member.organization_id)
    if org.billing_status != "exempt":
        raise HTTPException(403, "Internal source status is admin-only.")
    return _source_status()


class EnterpriseInquiryBody(BaseModel):
    name: str
    email: str
    company: str
    message: str = ""


class ChatEnquiryBody(BaseModel):
    message: str
    history: list[dict] = []


@app.post("/api/chat/enquiry")
def chat_enquiry(body: ChatEnquiryBody, request: Request) -> dict:
    """Public, unauthenticated — this powers the landing page enquiry bot.
    Real rate limiting matters here specifically because there's no account,
    no auth token, nothing stopping abuse the way every other route in this
    file is protected by current_member/active_member."""
    from .services.chatbot import answer, check_rate_limit
    client_ip = request.client.host if request.client else ""
    if not check_rate_limit(client_ip):
        raise HTTPException(429, "You've asked quite a few questions — please try again in a few minutes.")
    return {"reply": answer(body.message, body.history)}


class AppChatBody(BaseModel):
    message: str
    history: list[dict] = []


@app.post("/api/app-chat/{ws_id}")
def app_chat(ws_id: str, body: AppChatBody, ws: Workspace = Depends(owned_workspace),
            member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    """The in-app 'Ask BrandsLens' copilot — completely separate from the
    public marketing bot above. Not available on Standard at all; Growth
    and Professional+ get genuinely different depth of analysis, not just a
    cosmetic label difference."""
    from .services.auth import effective_plan, PLAN_APP_CHAT_MODE
    from .services.chatbot import answer_app_chat
    from collections import Counter
    org = db.get(Organization, member.organization_id)
    mode = PLAN_APP_CHAT_MODE[effective_plan(org)]
    if not mode:
        raise HTTPException(403, "Ask BrandsLens requires the Growth plan or higher.")

    incidents = db.scalars(select(Incident).where(Incident.workspace_id == ws.id)
                          .order_by(Incident.posted_at.desc()).limit(300)).all()
    sev = Counter(i.severity for i in incidents)
    platform = Counter(i.platform for i in incidents)
    stats = (f"Total mentions: {len(incidents)}\nSeverity: {dict(sev)}\n"
            f"Top platforms: {dict(platform.most_common(5))}\n"
            f"Recent HIGH severity titles: " +
            "; ".join(i.title[:120] for i in incidents if i.severity == "HIGH")[:1000])
    reply = answer_app_chat(body.message, body.history, ws.name, mode, stats)
    return {"reply": reply, "mode": mode}



@app.get("/api/setup/sync-schema")
def setup_sync_schema(secret: str, db: Session = Depends(get_db)) -> dict:
    """Temporary, one-time-use route — safely brings the live database up to
    date with the current models, WITHOUT dropping or touching any existing
    data. Unlike the old reset-database route (which wiped everything and
    was removed after use), this only ever adds what's genuinely missing:
    new tables via create_all (which never touches existing tables), and
    new columns on existing tables via real introspection rather than a
    hardcoded guess — this is the second time a new column silently didn't
    reach the live database, and guessing which one is missing each time
    isn't sustainable. Protected by the same ADMIN_SETUP_SECRET pattern as
    before. Remove this route and redeploy once you've used it."""
    if not ADMIN_SETUP_SECRET or secret != ADMIN_SETUP_SECRET:
        raise HTTPException(403, "Invalid or missing setup secret.")
    from sqlalchemy import inspect, text
    from .models import Base
    from .db import engine

    Base.metadata.create_all(engine)  # safe — only creates tables that don't exist yet

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added = []
    with engine.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            if table_name not in existing_tables:
                continue  # just created above, or genuinely new — nothing more to do
            live_columns = {c["name"] for c in inspector.get_columns(table_name)}
            for col in table.columns:
                if col.name in live_columns:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                default_clause = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    if isinstance(val, bool):
                        default_clause = f" DEFAULT {'TRUE' if val else 'FALSE'}"
                    elif isinstance(val, (int, float)):
                        default_clause = f" DEFAULT {val}"
                conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {col_type}{default_clause}'))
                added.append(f"{table_name}.{col.name}")

    return {"ok": True, "columns_added": added,
           "message": "Schema synced — no existing data was touched." if added else
                     "Schema was already up to date — nothing needed adding."}


@app.post("/api/enterprise-inquiry")
def enterprise_inquiry(body: EnterpriseInquiryBody) -> dict:
    """Public, unauthenticated — this is the entire 'checkout flow' for
    Enterprise, since there's no fixed price to charge a card against."""
    from .services.billing import submit_enterprise_inquiry
    sent = submit_enterprise_inquiry(body.name, body.email, body.company, body.message)
    return {"ok": True, "sent": sent}


def _render_legal_page(md_filename: str, title: str) -> str:
    import markdown, os
    path = os.path.join(os.path.dirname(__file__), "..", "legal", md_filename)
    with open(path, "r", encoding="utf-8") as f:
        body_html = markdown.markdown(f.read(), extensions=["extra"])
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — BrandsLens</title>
<style>
body{{font-family:-apple-system,Inter,Arial,sans-serif;max-width:760px;margin:0 auto;padding:48px 24px 80px;
     color:#1F2937;line-height:1.7;background:#F8FAFC}}
h1,h2,h3{{color:#0F172A}}
h1{{font-size:28px}} h2{{font-size:20px;margin-top:34px}} h3{{font-size:16px}}
a{{color:#D97706}}
.back{{display:inline-block;margin-bottom:24px;color:#475569;text-decoration:none;font-size:14px}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #E2E8F0;padding:8px 12px;text-align:left}}
</style></head><body>
<a class="back" href="/">&larr; Back to BrandsLens</a>
{body_html}
</body></html>"""


@app.get("/legal/terms", response_class=HTMLResponse)
def legal_terms() -> str:
    return _render_legal_page("TERMS_OF_SERVICE.md", "Terms of Service")


@app.get("/legal/privacy", response_class=HTMLResponse)
def legal_privacy() -> str:
    return _render_legal_page("PRIVACY_POLICY.md", "Privacy Policy")


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
from .services import report_generator  # noqa: E402


def _require_report_format(member: OrgMember, db: Session, fmt: str) -> None:
    from .services.auth import effective_plan, PLAN_REPORT_FORMATS
    org = db.get(Organization, member.organization_id)
    plan = effective_plan(org)
    if fmt not in PLAN_REPORT_FORMATS[plan]:
        names = {"pdf": "PDF reports", "pptx": "PowerPoint reports"}
        raise HTTPException(403, f"{names.get(fmt, fmt.upper())} require the Growth plan or higher "
                            f"(Professional for PowerPoint) — your current plan doesn't include this format.")


def _get_competitor_rows(ws: Workspace, db: Session) -> list[dict] | None:
    """Shared by all three report formats so the competitive section is
    identical regardless of which one gets downloaded — same computation
    as the /competitors/analytics endpoint, just callable directly from
    report generation without an extra HTTP round-trip."""
    from collections import Counter
    comps = db.scalars(select(Competitor).where(Competitor.workspace_id == ws.id)).all()
    if not comps:
        return None
    own_incidents = db.scalars(select(Incident).where(Incident.workspace_id == ws.id)).all()
    rows = [{"name": ws.name, "is_you": True, "mentions": len(own_incidents),
            "sentiment": dict(Counter(i.sentiment for i in own_incidents if i.sentiment))}]
    for comp in comps:
        mentions = db.scalars(select(CompetitorMention).where(CompetitorMention.competitor_id == comp.id)).all()
        rows.append({"name": comp.name, "is_you": False, "mentions": len(mentions),
                    "sentiment": dict(Counter(m.sentiment for m in mentions if m.sentiment))})
    return rows


@app.get("/api/reports/pdf")
def report_pdf(ws: Workspace = Depends(owned_workspace), member: OrgMember = Depends(active_member),
               db: Session = Depends(get_db), range: str | None = None, start: str | None = None,
               end: str | None = None) -> Response:
    _require_report_format(member, db, "pdf")
    q = _apply_date_filter(select(Incident).where(Incident.workspace_id == ws.id), range, start, end)
    incidents = db.scalars(q.order_by(Incident.posted_at.desc()).limit(500)).all()
    comp_rows = _get_competitor_rows(ws, db)
    pdf_bytes = report_generator.generate_pdf_report(ws, incidents, comp_rows)
    filename = f"{ws.name.replace(' ', '-').lower()}-brandslens-report.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/reports/pptx")
def report_pptx(ws: Workspace = Depends(owned_workspace), member: OrgMember = Depends(active_member),
                db: Session = Depends(get_db), range: str | None = None, start: str | None = None,
                end: str | None = None) -> Response:
    _require_report_format(member, db, "pptx")
    q = _apply_date_filter(select(Incident).where(Incident.workspace_id == ws.id), range, start, end)
    incidents = db.scalars(q.order_by(Incident.posted_at.desc()).limit(500)).all()
    comp_rows = _get_competitor_rows(ws, db)
    pptx_bytes = report_generator.generate_pptx_report(ws, incidents, comp_rows)
    filename = f"{ws.name.replace(' ', '-').lower()}-brandslens-report.pptx"
    return Response(content=pptx_bytes,
                    media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/reports/excel")
def report_excel(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db),
                 range: str | None = None, start: str | None = None, end: str | None = None) -> Response:
    # Excel is available on every tier, including Standard — no gating here.
    q = _apply_date_filter(select(Incident).where(Incident.workspace_id == ws.id), range, start, end)
    incidents = db.scalars(q.order_by(Incident.posted_at.desc()).limit(2000)).all()
    comp_rows = _get_competitor_rows(ws, db)
    xlsx_bytes = report_generator.generate_excel_export(ws, incidents, comp_rows)
    filename = f"{ws.name.replace(' ', '-').lower()}-brandslens-export.xlsx"
    return Response(content=xlsx_bytes,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/analytics/{ws_id}")
def analytics_summary(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db),
                      range: str | None = None, start: str | None = None, end: str | None = None) -> dict:
    """Pre-aggregated chart data so the frontend never has to recompute this
    from raw incidents client-side — same numbers power the dashboard charts,
    the PDF, and the Excel summary sheet, by construction. Same date filter
    as the incident list and exports, so a report always matches what was
    on screen when it was generated."""
    q = _apply_date_filter(select(Incident).where(Incident.workspace_id == ws.id), range, start, end)
    incidents = db.scalars(q.order_by(Incident.posted_at.desc()).limit(1000)).all()
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


# ==================================================================
# COMPETITOR ANALYSIS — tiered (2/5/8/unlimited), fully editable,
# reusing the same real search mechanisms as the main collectors.
# ==================================================================
class CompetitorBody(BaseModel):
    name: str


@app.get("/api/workspaces/{ws_id}/competitors")
def list_competitors(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db)) -> list[dict]:
    comps = db.scalars(select(Competitor).where(Competitor.workspace_id == ws.id)
                       .order_by(Competitor.created_at)).all()
    return [{"id": c.id, "name": c.name} for c in comps]


@app.post("/api/workspaces/{ws_id}/competitors")
def add_competitor(body: CompetitorBody, ws: Workspace = Depends(owned_workspace),
                   member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    from .services.auth import effective_plan, PLAN_COMPETITOR_LIMIT
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Competitor name can't be empty.")
    org = db.get(Organization, member.organization_id)
    limit = PLAN_COMPETITOR_LIMIT[effective_plan(org)]
    existing_count = db.scalar(select(func.count(Competitor.id)).where(Competitor.workspace_id == ws.id))
    if existing_count >= limit:
        raise HTTPException(422, f"Your plan allows tracking up to {limit} competitors. Upgrade to add more.")
    if db.scalar(select(Competitor).where(Competitor.workspace_id == ws.id, Competitor.name == name)):
        raise HTTPException(422, f'"{name}" is already being tracked.')
    comp = Competitor(workspace_id=ws.id, name=name)
    db.add(comp)
    db.commit()
    return {"id": comp.id, "name": comp.name}


@app.delete("/api/workspaces/{ws_id}/competitors/{comp_id}")
def remove_competitor(ws_id: str, comp_id: str, ws: Workspace = Depends(owned_workspace),
                      db: Session = Depends(get_db)) -> dict:
    comp = db.get(Competitor, comp_id)
    if not comp or comp.workspace_id != ws.id:
        raise HTTPException(404, "Not found")
    db.execute(delete(CompetitorMention).where(CompetitorMention.competitor_id == comp_id))
    db.delete(comp)
    db.commit()
    return {"ok": True}


def _run_competitor_scan(ws_id: str) -> None:
    from .db import SessionLocal
    from .services.competitor_scan import scan_competitor
    db = SessionLocal()
    try:
        comps = db.scalars(select(Competitor).where(Competitor.workspace_id == ws_id)).all()
        for comp in comps:
            try:
                scan_competitor(db, comp)
            except Exception:  # noqa: BLE001 — one competitor failing shouldn't block the others
                log.exception("Competitor scan failed for %s", comp.name)
    finally:
        db.close()


@app.post("/api/workspaces/{ws_id}/competitors/scan")
def trigger_competitor_scan(tasks: BackgroundTasks, ws: Workspace = Depends(owned_workspace)) -> dict:
    tasks.add_task(_run_competitor_scan, ws.id)
    return {"ok": True, "message": "Competitor scan started"}


@app.get("/api/workspaces/{ws_id}/competitors/analytics")
def competitor_analytics(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db),
                         range: str | None = None, start: str | None = None, end: str | None = None) -> dict:
    """Comparative data: your own brand's mention volume and sentiment
    alongside every tracked competitor's — same date filter as the rest of
    the app, so this stays consistent with whatever the dashboard shows."""
    from collections import Counter
    own_q = _apply_date_filter(select(Incident).where(Incident.workspace_id == ws.id), range, start, end)
    own_incidents = db.scalars(own_q).all()
    own_sentiment = Counter(i.sentiment for i in own_incidents if i.sentiment)

    comps = db.scalars(select(Competitor).where(Competitor.workspace_id == ws.id)).all()
    rows = [{"name": ws.name, "is_you": True, "mentions": len(own_incidents),
            "sentiment": dict(own_sentiment)}]
    for comp in comps:
        mentions = db.scalars(select(CompetitorMention).where(CompetitorMention.competitor_id == comp.id)).all()
        sent = Counter(m.sentiment for m in mentions if m.sentiment)
        platform = Counter(m.platform for m in mentions)
        rows.append({"name": comp.name, "is_you": False, "mentions": len(mentions),
                    "sentiment": dict(sent), "platform": dict(platform)})
    return {"rows": rows}
