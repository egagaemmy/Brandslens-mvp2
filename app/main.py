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
                     MediaRoomCase, MediaRoomAudit, Competitor, CompetitorMention,
                     EscalationContact, EscalationLog, ThreatCategory, BlogPost, NewsletterSubscriber, now_utc, aware)
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
            "reach": i.reach, "posted": i.posted_at.isoformat() if i.posted_at else None, "source": i.source,
            "found_historically": i.found_historically}


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
    human decision rather than silently overwriting the AI's original call.

    Manually marking something HIGH now genuinely escalates it into Media
    Room, the same as an AI-classified HIGH does — this was a real gap
    before: a human catching something the AI under-classified (exactly why
    this feature exists) silently never triggered the escalation workflow
    it's meant to."""
    if body.severity not in ("HIGH", "MEDIUM", "WATCH"):
        raise HTTPException(422, "Severity must be HIGH, MEDIUM, or WATCH.")
    inc = db.get(Incident, incident_id)
    if not inc:
        raise HTTPException(404, "Not found")
    ws = db.get(Workspace, inc.workspace_id)
    if not ws or ws.organization_id != member.organization_id:
        raise HTTPException(404, "Not found")
    was_escalated = inc.severity in ("HIGH", "MEDIUM")
    inc.severity = body.severity
    inc.severity_overridden = True
    db.commit()
    if body.severity in ("HIGH", "MEDIUM") and not was_escalated:
        existing_case = db.scalar(select(MediaRoomCase).where(MediaRoomCase.incident_id == inc.id))
        if not existing_case:
            from .services import media_room
            media_room.open_case(db, inc)
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
            log.warning("Historical scan requested for unknown workspace %s", ws_id)
            return
        log.info("Historical scan starting for %s — %s days back", ws.name, days_back)
        for mod in COLLECTORS:
            source = mod.__name__.split(".")[-1]
            run = ScanRun(workspace_id=ws.id, source=f"{source}_historical_{days_back}d")
            db.add(run); db.commit()
            try:
                kwargs = {"days_back": days_back} if source in HISTORICAL_CAPABLE else {}
                candidates = mod.collect(db, ws, **kwargs)
                summary = pipeline.ingest_candidates(db, ws, candidates, source=source, found_historically=True)
                run.finished_at = now_utc()
                run.candidates, run.new_incidents, run.high_count = summary["candidates"], summary["new"], summary["high"]
                log.info("historical/%s / %s (%sd back): %s", source, ws.name, days_back, summary)
            except Exception as e:  # noqa: BLE001
                run.error = str(e)[:2000]; run.finished_at = now_utc()
                log.exception("Historical scan failed for %s / %s (%sd back)", source, ws.name, days_back)
            db.commit()
        log.info("Historical scan finished for %s", ws.name)
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
# THREAT CATEGORIES — a workspace's own editable list of what counts as
# relevant to escalate on. Seeded with sensible defaults at creation, but
# genuinely editable after that: what matters to a fintech (regulator
# notification) is rarely what matters to an FMCG brand (counterfeit
# product reports), and neither should be stuck with only the built-in
# set. This governs what shows up as an escalation-contact category —
# it's deliberately separate from how an incident gets auto-classified,
# which keeps working exactly as it always has.
# ==================================================================
class ThreatCategoryBody(BaseModel):
    key: str
    label: str


@app.get("/api/workspaces/{ws_id}/threat-categories")
def list_threat_categories(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(ThreatCategory).where(ThreatCategory.workspace_id == ws.id)
                      .order_by(ThreatCategory.created_at)).all()
    return [{"id": c.id, "key": c.key, "label": c.label} for c in rows]


@app.post("/api/workspaces/{ws_id}/threat-categories")
def add_threat_category(body: ThreatCategoryBody, ws: Workspace = Depends(owned_workspace),
                        member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    if member.role == "member":
        raise HTTPException(403, "Only an Owner or Team Lead can edit threat categories.")
    key = body.key.strip().lower().replace(" ", "_")
    label = body.label.strip()
    if not key or not label:
        raise HTTPException(422, "Both a key and a label are required.")
    existing = db.scalar(select(ThreatCategory).where(ThreatCategory.workspace_id == ws.id, ThreatCategory.key == key))
    if existing:
        raise HTTPException(422, f"A category with key '{key}' already exists for this workspace.")
    cat = ThreatCategory(workspace_id=ws.id, key=key, label=label)
    db.add(cat)
    db.commit()
    return {"id": cat.id, "key": cat.key, "label": cat.label}


@app.delete("/api/workspaces/{ws_id}/threat-categories/{category_id}")
def remove_threat_category(category_id: str, ws: Workspace = Depends(owned_workspace),
                           member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    if member.role == "member":
        raise HTTPException(403, "Only an Owner or Team Lead can edit threat categories.")
    cat = db.get(ThreatCategory, category_id)
    if not cat or cat.workspace_id != ws.id:
        raise HTTPException(404, "That category wasn't found for this workspace.")
    if cat.key == "general":
        raise HTTPException(422, "The 'general' category can't be removed — it's the fallback every escalation contact list falls back to.")
    db.delete(cat)
    db.commit()
    return {"ok": True}


# ==================================================================
# ESCALATION CONTACTS — configured by the workspace admin, not the AI.
# This is what actually lets Media Room tell a team WHO to escalate to.
# ==================================================================
class EscalationContactBody(BaseModel):
    name: str
    title: str = ""
    email: str
    category: str = "general"


@app.get("/api/workspaces/{ws_id}/escalation-contacts")
def list_escalation_contacts(ws: Workspace = Depends(owned_workspace), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(EscalationContact).where(EscalationContact.workspace_id == ws.id)
                      .order_by(EscalationContact.created_at)).all()
    return [{"id": c.id, "name": c.name, "title": c.title, "email": c.email, "category": c.category} for c in rows]


@app.post("/api/workspaces/{ws_id}/escalation-contacts")
def add_escalation_contact(body: EscalationContactBody, ws: Workspace = Depends(owned_workspace),
                           member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    if member.role == "member":
        raise HTTPException(403, "Only an Owner or Team Lead can configure escalation contacts.")
    valid_categories = {c.key for c in db.scalars(
        select(ThreatCategory).where(ThreatCategory.workspace_id == ws.id))} | {"general"}
    if body.category not in valid_categories:
        raise HTTPException(422, f"Category must be one of: {', '.join(sorted(valid_categories))}")
    if not body.name.strip() or not body.email.strip():
        raise HTTPException(422, "Name and email are required.")
    contact = EscalationContact(workspace_id=ws.id, name=body.name.strip(), title=body.title.strip(),
                               email=body.email.strip(), category=body.category)
    db.add(contact)
    db.commit()
    return {"id": contact.id, "name": contact.name, "title": contact.title,
           "email": contact.email, "category": contact.category}


@app.delete("/api/workspaces/{ws_id}/escalation-contacts/{contact_id}")
def remove_escalation_contact(ws_id: str, contact_id: str, ws: Workspace = Depends(owned_workspace),
                              member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    if member.role == "member":
        raise HTTPException(403, "Only an Owner or Team Lead can configure escalation contacts.")
    contact = db.get(EscalationContact, contact_id)
    if not contact or contact.workspace_id != ws.id:
        raise HTTPException(404, "Not found")
    db.delete(contact)
    db.commit()
    return {"ok": True}


class ComposeEscalationBody(BaseModel):
    template_key: str
    contact_id: str
    subject: str
    body: str


@app.post("/api/media-room/cases/{case_id}/escalate")
def compose_escalation(case_id: str, body: ComposeEscalationBody,
                       member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    case = _case_in_org(case_id, member, db)
    contact = db.get(EscalationContact, body.contact_id)
    if not contact or contact.workspace_id != case.workspace_id:
        raise HTTPException(404, "That contact wasn't found for this workspace.")
    log_entry = media_room.send_escalation(db, case, body.template_key, contact.name, contact.email,
                                           body.subject, body.body, member.name)
    return {"ok": True, "email_sent": log_entry.email_sent,
           "message": "Sent." if log_entry.email_sent else "Logged — email delivery isn't configured yet, but this escalation is fully recorded."}


@app.get("/api/media-room/cases/{case_id}/escalations")
def list_case_escalations(case_id: str, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> list[dict]:
    case = _case_in_org(case_id, member, db)
    rows = db.scalars(select(EscalationLog).where(EscalationLog.case_id == case.id)
                      .order_by(EscalationLog.created_at.desc())).all()
    return [{"id": r.id, "template_key": r.template_key, "recipient_name": r.recipient_name,
            "recipient_email": r.recipient_email, "subject": r.subject, "body": r.body,
            "sent_by_name": r.sent_by_name, "email_sent": r.email_sent,
            "created_at": r.created_at.isoformat()} for r in rows]


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


@app.get("/api/setup/backfill-rss-feeds")
def setup_backfill_rss_feeds(secret: str, db: Session = Depends(get_db)) -> dict:
    """Temporary, one-time-use route — the default RSS feed list only ever
    gets applied at workspace CREATION time (see auth.signup() and
    auth.add_workspace()), so any workspace created before that feature
    existed never received it and never will on its own. This backfills
    every existing workspace, adding only whichever of the default feeds
    it doesn't already have — anything a workspace's own team has since
    added or deliberately removed is left completely untouched. Safe to
    call more than once: a workspace that already has all the defaults
    is a no-op. Protected by the same ADMIN_SETUP_SECRET pattern as the
    other setup routes."""
    if not ADMIN_SETUP_SECRET or secret != ADMIN_SETUP_SECRET:
        raise HTTPException(403, "Invalid or missing setup secret.")

    updated = []
    workspaces = db.scalars(select(Workspace)).all()
    for ws in workspaces:
        current = set(ws.rss_feeds or [])
        missing = [f for f in auth.DEFAULT_RSS_FEEDS if f not in current]
        if missing:
            ws.rss_feeds = list(ws.rss_feeds or []) + missing
            updated.append({"workspace": ws.name, "feeds_added": len(missing)})
    db.commit()
    return {"ok": True, "workspaces_updated": updated,
           "message": f"Backfilled {len(updated)} workspace(s)." if updated else
                     "Every workspace already had the full default feed list — nothing needed adding."}


@app.get("/api/setup/seed-blog")
def setup_seed_blog(secret: str, db: Session = Depends(get_db)) -> dict:
    """Temporary, one-time-use route — publishes the four launch blog posts
    directly to the live database. Idempotent: checks each post's slug
    before inserting, so it's safe to call more than once without creating
    duplicates. Protected by the same ADMIN_SETUP_SECRET pattern as the
    other setup routes. Safe to remove after use, or leave in place since
    calling it again with the same posts already published is a no-op."""
    if not ADMIN_SETUP_SECRET or secret != ADMIN_SETUP_SECRET:
        raise HTTPException(403, "Invalid or missing setup secret.")

    posts = [
        {
            "title": "How to Choose the Best Media Monitoring Tool for Your Brand in 2026",
            "excerpt": "A practical buyer's framework for evaluating media monitoring tools: what actually matters, what's marketing noise, and the questions worth asking before you sign anything.",
            "meta_description": "A practical guide to choosing the best media monitoring tool for your brand: real-time coverage, AI severity scoring, fraud detection, and escalation workflows explained.",
            "body_html": """<p>Most teams researching a media monitoring tool start with the same question: which one has the most sources? It's a reasonable place to start, and also the wrong question to lead with. Coverage matters, but coverage without judgement just produces a longer list of things nobody has time to read.</p>
<p>Here is a more useful framework: five questions worth asking before you sign anything, drawn from what actually separates a genuinely useful <b>media monitoring tool</b> from a dashboard that quietly gets ignored after the first month.</p>
<h2>1. Does it tell you what to do, or just what was said?</h2>
<p>Traditional listening tools are built around a simple loop: collect mentions, display them, let a human sort through the noise. That loop works reasonably well when volume is low. It breaks down the moment your brand is mentioned fifty times a day across news, forums, and social platforms, most of it irrelevant, a small fraction genuinely urgent.</p>
<p>A serious evaluation should include a direct test: feed the tool a mix of mentions, some routine, some genuinely concerning, and see whether it can tell the difference without a person doing all the work manually. AI severity scoring, done well, is the difference between a monitoring feed and an actual early warning system.</p>
<h2>2. Can it track media mentions in the languages your audience actually uses?</h2>
<p>A tool built primarily around English-language Western media will miss a meaningful share of what matters to a brand operating across African markets: pidgin, mixed-language forum posts, regional news outlets that never show up in a generic search index. If a vendor can't speak to this directly, treat that as a real gap, not a minor limitation.</p>
<h2>3. Does it separate "mentioned" from "at risk"?</h2>
<p>Being mentioned is neutral. Being impersonated, defrauded, or the subject of a coordinated misinformation push is not. The <b>best tools for media monitoring and tracking</b> distinguish between the two categories entirely, not just by keyword matching but by looking for the actual patterns of fraud and impersonation: look-alike domains, cloned social accounts, fabricated quotes. This is a fundamentally different capability from sentiment tracking, and most listening tools were never built to do it.</p>
<h2>4. What happens after something serious is found?</h2>
<p>This is the question most buyer's guides skip entirely, and it's often the most important one. A monitoring tool that surfaces a genuine crisis and then leaves your team to figure out what happens next, manually, in a spreadsheet or a scattered set of Slack messages, has only done half the job. Ask specifically: is there a defined escalation path? Can severity trigger a real workflow with SLA timers and a named owner, or does everything still depend on someone noticing in time?</p>
<h2>5. Can you actually prove what happened, later?</h2>
<p>If a serious incident ever ends up being reviewed by a regulator, a board, or your own legal team, "we saw it and dealt with it" needs to be demonstrable, not just remembered. Look for genuine audit trails: a record of who did what, when, that can't be quietly edited after the fact.</p>
<h2>A shorter way to say all of this</h2>
<p>The tools worth paying for treat monitoring as the beginning of a process, not the whole product. Coverage gets you visibility. Judgement gets you an early warning. A real workflow is what actually protects the brand when it matters.</p>""",
        },
        {
            "title": "The Hidden Cost of Not Tracking Online Mentions: A Guide for African Brands",
            "excerpt": "The mentions nobody's watching are rarely harmless. A look at what actually happens between the first quiet signal and the moment a brand notices, and what it costs in between.",
            "meta_description": "Why online mentions tracking matters for African brands: the real cost of delayed detection, from fraud losses to reputational damage, and what proactive monitoring actually prevents.",
            "body_html": """<p>Every brand of any size is being discussed somewhere right now, in places nobody on the team is currently looking. Most of that conversation is harmless. Some of it is not, and the gap between those two categories is exactly where damage accumulates, quietly, before anyone notices.</p>
<p>This isn't a hypothetical risk. It's a predictable pattern, and understanding it is the first step toward a genuine <b>online mentions tracking</b> discipline rather than an occasional Google search when something already feels wrong.</p>
<h2>The pattern almost always looks the same</h2>
<p>A fraudulent account opens, using a brand's name and logo, promising an opportunity that doesn't exist. It gathers a small following quietly over days or weeks. By the time a customer complaint reaches the actual company, often through a support email or a frustrated comment on a legitimate page, real financial harm has already happened to real people, and the brand's name is attached to it whether it likes it or not.</p>
<p>The same shape repeats with look-alike domains, misquoted statements picked up and repeated by secondary outlets, and coordinated negative campaigns that start small and specific before becoming general and hard to trace back to their origin.</p>
<h2>What "hidden cost" actually means in practice</h2>
<ul>
<li><b>Financial exposure</b>: fraud conducted under a brand's name creates victims who reasonably expect the brand to help, refund, or explain, regardless of the fact that the company had no direct involvement.</li>
<li><b>Regulatory exposure</b>: in regulated sectors, a delayed or absent response to a known impersonation scheme is itself a compliance question, not just a reputational one.</li>
<li><b>Compounding narrative</b>: the longer a false or damaging claim circulates without a documented, timely response, the more it reads, fairly or not, as tacit confirmation.</li>
<li><b>Internal cost</b>: teams without a monitoring discipline end up reacting to crises days later than they could have, under far more pressure than a same-day response would have required.</li>
</ul>
<h2>Why this matters more, not less, in fast-growing markets</h2>
<p>Brands operating across Nigeria and wider African markets face a specific version of this problem: media landscapes that move quickly across WhatsApp, Twitter/X, and regional forums, often faster than traditional press monitoring was ever built to follow, combined with digital financial products that make brand impersonation for fraud a genuinely lucrative crime, not just a nuisance.</p>
<p>A generic, Western-built listening tool frequently misses exactly the channels where this activity actually happens.</p>
<h2>What proactive tracking changes</h2>
<p>The value of consistent monitoring isn't found in the mentions that were always going to be fine. It's found in the handful, per quarter, per year, that would otherwise have gone unnoticed for days: the fraudulent account before it gathers a hundred victims instead of ten, the look-alike domain before it's indexed and trusted, the misquote before three other outlets have already repeated it as fact.</p>
<p>None of that requires reading everything. It requires a system that reliably surfaces the handful of things that matter, quickly enough to still make a difference.</p>""",
        },
        {
            "title": "Brand Impersonation and Fraud: How to Detect and Respond Before It Costs You",
            "excerpt": "Look-alike domains and cloned accounts follow recognisable patterns. A practical guide to spotting them early, and building a response that actually protects the people being targeted.",
            "meta_description": "How to detect brand impersonation and fraud early: look-alike domain patterns, cloned account warning signs, and a practical response framework for brand protection teams.",
            "body_html": """<p>Brand impersonation rarely announces itself. It shows up as a slightly-off domain name, a social account with the right logo and the wrong intentions, a message that reads almost, but not quite, like something the company would actually say. Recognising the pattern early is the entire difference between a contained incident and a genuine crisis.</p>
<h2>What look-alike domains actually look like</h2>
<p>Fraudulent domains are built to survive a glance, not close inspection. Common patterns include:</p>
<ul>
<li>Character substitution: a zero for an "o", a lowercase "l" for an "i"</li>
<li>Added or dropped words: "brandname-support" or "brandname-ng" instead of the real domain</li>
<li>Different top-level domains entirely, especially ones that read as plausible in a specific market</li>
<li>Homoglyphs: characters from other alphabets that render as visually near-identical to Latin letters</li>
</ul>
<p>One genuinely useful signal, often overlooked: a domain's actual history. A legitimate business's domain typically has years of archived history. A freshly registered look-alike domain usually has none at all, and that absence is itself informative, not just a technicality. Checking a domain against public web archives before assuming legitimacy is a small habit that catches a meaningful share of impersonation attempts early.</p>
<h2>Cloned accounts follow a similar shape</h2>
<p>They tend to appear quickly, use official brand assets without variation, and target a narrow, urgent action: send payment, click this link, confirm these details. The account is rarely trying to build a long-term audience. It's built to extract something specific before it gets reported and removed.</p>
<h2>A response framework that actually holds up</h2>
<h3>1. Verify quickly, but verify</h3>
<p>Confirm the finding is genuine before acting publicly. A false alarm handled as a real one erodes trust in the process itself.</p>
<h3>2. Document immediately</h3>
<p>Screenshot everything, record the domain's registration and hosting details where available, and timestamp the discovery. This record matters later, for platform takedown requests, for regulators, and for any customers who were affected.</p>
<h3>3. Escalate by severity, not by instinct</h3>
<p>Not every impersonation attempt needs the same response speed. A dormant look-alike domain with no active content is a different priority from an account actively soliciting payments right now. A defined severity tier, with a real time target attached to each level, keeps the response proportionate and fast where it needs to be.</p>
<h3>4. Notify the people actually at risk</h3>
<p>Internal awareness isn't the goal. The goal is making sure anyone who might encounter the fraudulent version has a way to recognise it, through official channels, before they act on it.</p>
<h3>5. Keep a real record of what was done</h3>
<p>Not a memory of what happened, an actual, tamper-evident log: who found it, when, what action was taken, by whom. This is the record that holds up under later scrutiny, whether that's a board question or a regulatory one.</p>
<h2>The underlying principle</h2>
<p>Fraud built on a brand's name moves fast precisely because it counts on nobody watching closely enough, early enough. A consistent detection habit, paired with a response process that doesn't have to be improvised under pressure, closes that window before it costs anyone something real.</p>""",
        },
        {
            "title": "From Detection to Resolution: Building a Real Crisis Escalation Workflow",
            "excerpt": "Finding a problem is only the first half of the job. A look at what separates a genuine escalation workflow from a Slack channel and good intentions.",
            "meta_description": "How to build a real crisis escalation workflow for brand and reputation incidents: SLA timers, named ownership, approval steps, and audit trails that actually hold up.",
            "body_html": """<p>Most organisations discover their escalation process doesn't really exist at the worst possible moment: during an actual incident, when someone finally asks out loud, "who is supposed to handle this?" and the honest answer is nobody quite knows.</p>
<p>A monitoring system that detects a serious incident and stops there has only completed half the job. What happens in the hours immediately after detection is usually what determines whether an incident stays small or becomes a genuine crisis.</p>
<h2>Why "someone will see it eventually" isn't a process</h2>
<p>Informal escalation, a message in a shared channel, an email forwarded to a manager, works fine until the person who usually handles it is unavailable, or the incident happens outside normal hours, or three people each assume someone else has already responded. None of that is a hypothetical. It's the default failure mode of any process that depends entirely on individual attention rather than a defined structure.</p>
<h2>What a real workflow actually includes</h2>
<h3>Severity-based response times, not a single standard</h3>
<p>A confirmed fraud alert soliciting payments right now is not the same priority as a low-signal mention worth watching. Attaching a genuine time target to each severity level, and making that target visible, turns "we should get to this soon" into an actual accountable commitment.</p>
<h3>Named ownership per category, decided in advance</h3>
<p>Who handles a fraud alert. Who handles a regulatory notification. Who handles a domain takedown request. These should be decided calmly, in advance, by the people who actually run the organisation, not improvised in the middle of an incident by whoever happens to be online. A workflow that lets an admin configure named contacts per category, ahead of time, is doing something a generic alert system cannot.</p>
<h3>A defined path through the incident, not just a status</h3>
<p>Detected, under review, classified, escalated, resolved: a real workflow moves an incident through defined stages, so at any point anyone can answer "where are we on this" without a meeting.</p>
<h3>Approval where it genuinely matters</h3>
<p>Some categories of response, particularly anything touching regulators or public statements, should require a deliberate sign-off step before anything goes out. This isn't bureaucracy for its own sake. It's the difference between a considered response and something sent in the heat of the moment that the organisation later has to walk back.</p>
<h3>A record that can't quietly change after the fact</h3>
<p>When an incident is later reviewed, internally or by a regulator, the value of the record depends entirely on whether it can be trusted. A genuinely tamper-evident audit trail, where every action is chained to the one before it, is what makes "here is exactly what we did, and when" a demonstrable fact rather than a recollection.</p>
<h2>The test worth applying</h2>
<p>Ask the team a simple question: if a serious incident happened right now, this minute, would everyone involved know immediately what happens next, who owns it, and how fast a response is expected? If the honest answer involves any version of "we'd figure it out," that's not a workflow. It's a plan to improvise under pressure, and pressure is exactly the wrong time to be improvising.</p>""",
        },
    ]

    created = []
    for p in posts:
        slug = _slugify(p["title"])
        existing = db.scalar(select(BlogPost).where(BlogPost.slug == slug))
        if existing:
            continue
        post = BlogPost(slug=slug, title=p["title"], excerpt=p["excerpt"], body_html=p["body_html"],
                        meta_description=p["meta_description"], author_name="BrandsLens Team",
                        status="published", published_at=now_utc())
        db.add(post)
        created.append(slug)
    db.commit()
    return {"ok": True, "created": created,
           "message": f"{len(created)} post(s) published." if created else "All four posts were already published — nothing to do."}


@app.post("/api/enterprise-inquiry")
def enterprise_inquiry(body: EnterpriseInquiryBody) -> dict:
    """Public, unauthenticated — this is the entire 'checkout flow' for
    Enterprise, since there's no fixed price to charge a card against."""
    from .services.billing import submit_enterprise_inquiry
    sent = submit_enterprise_inquiry(body.name, body.email, body.company, body.message)
    return {"ok": True, "sent": sent}


class NewsletterSubscribeBody(BaseModel):
    email: str
    source: str = "website"


@app.post("/api/newsletter/subscribe")
def newsletter_subscribe(body: NewsletterSubscribeBody, db: Session = Depends(get_db)) -> dict:
    """Public, unauthenticated. BrandsLens is always the source of truth —
    stored here first, no matter what. Forwarding to an external email
    marketing platform (via NEWSLETTER_WEBHOOK_URL) is a bonus, never a
    dependency: if that forward fails or isn't configured, the signup is
    still safely recorded and nothing is lost."""
    import re
    from .config import NEWSLETTER_WEBHOOK_URL
    email = body.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(422, "That doesn't look like a valid email address.")
    existing = db.scalar(select(NewsletterSubscriber).where(NewsletterSubscriber.email == email))
    if existing:
        return {"ok": True, "already_subscribed": True}
    sub = NewsletterSubscriber(email=email, source=body.source)
    db.add(sub)
    db.commit()
    if NEWSLETTER_WEBHOOK_URL:
        try:
            httpx.post(NEWSLETTER_WEBHOOK_URL, json={"email": email, "source": body.source}, timeout=8)
        except Exception:  # noqa: BLE001 — the subscriber is already safely stored regardless
            log.warning("Newsletter webhook forward failed for a new signup — subscriber is still saved locally")
    return {"ok": True, "already_subscribed": False}


@app.get("/api/newsletter/subscribers")
def list_newsletter_subscribers(member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    # This is BrandsLens's own marketing subscriber list, not workspace
    # data — it must never be visible to a customer just because their own
    # organization happens to be on a particular plan. Only the actual
    # BrandsLens admin account (billing_status == "exempt") should ever see it.
    org = db.get(Organization, member.organization_id)
    if org.billing_status != "exempt":
        raise HTTPException(403, "Not available on this account.")
    rows = db.scalars(select(NewsletterSubscriber).order_by(NewsletterSubscriber.subscribed_at.desc())).all()
    return {"total": len(rows), "subscribers": [{"email": r.email, "subscribed_at": r.subscribed_at.isoformat(),
                                                  "source": r.source} for r in rows]}


@app.get("/api/newsletter/export.csv")
def export_newsletter_csv(member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> Response:
    org = db.get(Organization, member.organization_id)
    if org.billing_status != "exempt":
        raise HTTPException(403, "Not available on this account.")
    rows = db.scalars(select(NewsletterSubscriber).order_by(NewsletterSubscriber.subscribed_at.desc())).all()
    csv_lines = ["email,subscribed_at,source"] + [f"{r.email},{r.subscribed_at.isoformat()},{r.source}" for r in rows]
    return Response(content="\n".join(csv_lines), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="brandslens-newsletter-subscribers.csv"'})


def _render_legal_page(markdown_text: str, title: str) -> str:
    import markdown
    body_html = markdown.markdown(markdown_text, extensions=["extra"])
    home_link = FRONTEND_ORIGIN if FRONTEND_ORIGIN != "*" else "https://brandslens.app"
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
<a class="back" href="{home_link}">&larr; Back to BrandsLens</a>
{body_html}
</body></html>"""


@app.get("/legal/terms", response_class=HTMLResponse)
def legal_terms() -> str:
    from .legal_content import TERMS_OF_SERVICE
    return _render_legal_page(TERMS_OF_SERVICE, "Terms of Service")


@app.get("/legal/privacy", response_class=HTMLResponse)
def legal_privacy() -> str:
    from .legal_content import PRIVACY_POLICY
    return _render_legal_page(PRIVACY_POLICY, "Privacy Policy")


# ==================================================================
# BLOG — served with real, individually crawlable URLs, since that's
# the entire SEO point of having a blog at all. Public routes render
# actual HTML with per-post meta tags; the admin API (JSON) powers the
# in-app editor.
# ==================================================================
def _blog_page_wrapper(title: str, meta_description: str, canonical_path: str, body: str,
                       og_image: str = "") -> str:
    og_image_tag = f'<meta property="og:image" content="{og_image}">' if og_image else ""
    home_link = FRONTEND_ORIGIN if FRONTEND_ORIGIN != "*" else "https://brandslens.app"
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{meta_description}">
<link rel="canonical" href="https://brandslens.app{canonical_path}">
<meta property="og:type" content="article">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{meta_description}">
<meta property="og:url" content="https://brandslens.app{canonical_path}">
{og_image_tag}
<style>
body{{font-family:-apple-system,Inter,Arial,sans-serif;margin:0;color:#1F2937;background:#F8FAFC}}
.wrap{{max-width:760px;margin:0 auto;padding:0 20px 80px}}
.topbar{{background:#0B0F17;padding:18px 20px;margin-bottom:40px}}
.topbar a{{color:#fff;text-decoration:none;font-weight:700;font-size:17px}}
.topbar .lens{{color:#D97706}}
h1{{font-size:34px;color:#0F172A;line-height:1.25;margin-bottom:8px}}
.meta{{color:#64748B;font-size:13.5px;margin-bottom:28px}}
.cover{{width:100%;border-radius:12px;margin-bottom:28px;display:block}}
.body-content{{font-size:16.5px;line-height:1.8}}
.body-content h2{{font-size:23px;margin-top:38px;color:#0F172A}}
.body-content h3{{font-size:18px;margin-top:28px;color:#0F172A}}
.body-content p{{margin:16px 0}}
.body-content img{{max-width:100%;border-radius:8px}}
.body-content a{{color:#D97706}}
.body-content ul,.body-content ol{{padding-left:24px}}
.share-row{{display:flex;gap:12px;margin-top:44px;padding-top:24px;border-top:1px solid #E2E8F0}}
.share-row a{{display:inline-flex;align-items:center;padding:8px 16px;border-radius:8px;background:#0F172A;
             color:#fff;text-decoration:none;font-size:13px;font-weight:600}}
.post-card{{background:#fff;border-radius:14px;padding:22px;margin-bottom:18px;border:1px solid #E5E7EB;
           text-decoration:none;display:block;color:inherit}}
.post-card h2{{font-size:21px;color:#0F172A;margin:0 0 8px}}
.post-card p{{color:#475569;font-size:14.5px;margin:0}}
.post-card .meta{{margin-bottom:10px;font-size:12.5px}}
video-embed{{aspect-ratio:16/9;width:100%;display:block;border-radius:10px;margin-bottom:28px;border:none}}
</style>
</head><body>
<div class="topbar"><a href="{home_link}">Brands<span class="lens">Lens</span></a></div>
<div class="wrap">{body}</div>
</body></html>"""


@app.get("/blog", response_class=HTMLResponse)
def blog_listing(db: Session = Depends(get_db)) -> str:
    posts = db.scalars(select(BlogPost).where(BlogPost.status == "published")
                       .order_by(BlogPost.published_at.desc())).all()
    if not posts:
        body = "<h1>The BrandsLens Blog</h1><p>New posts are on their way.</p>"
    else:
        cards = "".join(f"""<a class="post-card" href="/blog/{p.slug}">
            <div class="meta">{p.published_at.strftime('%d %B %Y') if p.published_at else ''} · {p.author_name}</div>
            <h2>{p.title}</h2><p>{p.excerpt}</p></a>""" for p in posts)
        body = f"<h1>The BrandsLens Blog</h1><p style='color:#64748B;margin-bottom:32px'>Media monitoring, brand reputation, and fraud prevention — for teams protecting what they've built.</p>{cards}"
    return _blog_page_wrapper("The BrandsLens Blog — Media Monitoring & Brand Reputation Insights",
                              "Practical guidance on media monitoring, brand reputation, and fraud prevention for teams operating in African markets.",
                              "/blog", body)


@app.get("/blog/{slug}", response_class=HTMLResponse)
def blog_post(slug: str, db: Session = Depends(get_db)) -> str:
    post = db.scalar(select(BlogPost).where(BlogPost.slug == slug, BlogPost.status == "published"))
    if not post:
        return _blog_page_wrapper("Post not found — BrandsLens Blog", "This post isn't available.", f"/blog/{slug}",
                                  "<h1>Post not found</h1><p>This post may have been unpublished or the link is incorrect. <a href='/blog'>Back to the blog</a>.</p>")
    video_embed = f'<iframe video-embed src="{post.video_url}" allowfullscreen></iframe>' if post.video_url else ""
    cover = f'<img class="cover" src="{post.cover_image}" alt="{post.title}">' if post.cover_image else ""
    share_url = f"https://brandslens.app/blog/{post.slug}"
    body = f"""<h1>{post.title}</h1>
    <div class="meta">{post.published_at.strftime('%d %B %Y') if post.published_at else ''} · {post.author_name}</div>
    {cover}{video_embed}
    <div class="body-content">{post.body_html}</div>
    <div class="share-row">
      <a href="https://www.linkedin.com/sharing/share-offsite/?url={share_url}" target="_blank" rel="noopener">Share on LinkedIn</a>
      <a href="https://twitter.com/intent/tweet?url={share_url}&text={post.title}" target="_blank" rel="noopener">Share on X</a>
      <a href="https://www.facebook.com/sharer/sharer.php?u={share_url}" target="_blank" rel="noopener">Share on Facebook</a>
    </div>"""
    return _blog_page_wrapper(f"{post.title} — BrandsLens Blog", post.meta_description or post.excerpt,
                              f"/blog/{post.slug}", body, og_image=post.cover_image)


def _slugify(title: str) -> str:
    import re
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")[:200]


class BlogPostBody(BaseModel):
    title: str
    excerpt: str = ""
    body_html: str = ""
    cover_image: str = ""
    video_url: str = ""
    meta_description: str = ""
    author_name: str = "BrandsLens Team"


def _require_exempt(member: OrgMember, db: Session) -> None:
    org = db.get(Organization, member.organization_id)
    if org.billing_status != "exempt":
        raise HTTPException(403, "The blog is managed by the BrandsLens team.")


@app.get("/api/admin/blog/posts")
def admin_list_blog_posts(member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> list[dict]:
    _require_exempt(member, db)
    posts = db.scalars(select(BlogPost).order_by(BlogPost.created_at.desc())).all()
    return [_blog_post_dict(p) for p in posts]


def _blog_post_dict(p: BlogPost) -> dict:
    return {"id": p.id, "slug": p.slug, "title": p.title, "excerpt": p.excerpt, "body_html": p.body_html,
           "cover_image": p.cover_image, "video_url": p.video_url, "meta_description": p.meta_description,
           "author_name": p.author_name, "status": p.status,
           "published_at": p.published_at.isoformat() if p.published_at else None,
           "created_at": p.created_at.isoformat()}


@app.post("/api/admin/blog/posts")
def admin_create_blog_post(body: BlogPostBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_exempt(member, db)
    if not body.title.strip():
        raise HTTPException(422, "Title is required.")
    if len(body.cover_image) > 3_000_000:
        raise HTTPException(422, "That image is too large — please use a smaller file or an external image URL.")
    slug_base = _slugify(body.title)
    slug = slug_base
    i = 2
    while db.scalar(select(BlogPost).where(BlogPost.slug == slug)):
        slug = f"{slug_base}-{i}"
        i += 1
    post = BlogPost(slug=slug, title=body.title.strip(), excerpt=body.excerpt, body_html=body.body_html,
                    cover_image=body.cover_image, video_url=body.video_url,
                    meta_description=body.meta_description, author_name=body.author_name or "BrandsLens Team")
    db.add(post)
    db.commit()
    return _blog_post_dict(post)


@app.patch("/api/admin/blog/posts/{post_id}")
def admin_update_blog_post(post_id: str, body: BlogPostBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_exempt(member, db)
    post = db.get(BlogPost, post_id)
    if not post:
        raise HTTPException(404, "Not found")
    if len(body.cover_image) > 3_000_000:
        raise HTTPException(422, "That image is too large — please use a smaller file or an external image URL.")
    post.title, post.excerpt, post.body_html = body.title.strip(), body.excerpt, body.body_html
    post.cover_image, post.video_url = body.cover_image, body.video_url
    post.meta_description, post.author_name = body.meta_description, body.author_name or "BrandsLens Team"
    post.updated_at = now_utc()
    db.commit()
    return _blog_post_dict(post)


@app.post("/api/admin/blog/posts/{post_id}/publish")
def admin_publish_blog_post(post_id: str, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_exempt(member, db)
    post = db.get(BlogPost, post_id)
    if not post:
        raise HTTPException(404, "Not found")
    post.status = "published"
    if not post.published_at:
        post.published_at = now_utc()
    db.commit()
    return _blog_post_dict(post)


@app.post("/api/admin/blog/posts/{post_id}/unpublish")
def admin_unpublish_blog_post(post_id: str, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_exempt(member, db)
    post = db.get(BlogPost, post_id)
    if not post:
        raise HTTPException(404, "Not found")
    post.status = "draft"
    db.commit()
    return _blog_post_dict(post)


@app.delete("/api/admin/blog/posts/{post_id}")
def admin_delete_blog_post(post_id: str, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_exempt(member, db)
    post = db.get(BlogPost, post_id)
    if not post:
        raise HTTPException(404, "Not found")
    db.delete(post)
    db.commit()
    return {"ok": True}


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
