"""routes_trial.py: the free trial. Public sign-up (business email + emailed code), the in-app status, and the Super Admin controls.

Admin routes are for BrandsLens team accounts only. Public routes are rate limited, and the emailed code is what proves a person owns the address."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .deps import current_member
from .models import Organization, OrgMember, TrialSignup, Workspace
from .services import trial
from .services.ratelimit import RateLimiter

router = APIRouter()
_start_ip, _start_email = RateLimiter(20, 3600), RateLimiter(5, 3600)     # per IP (an office may share one) and per address, each hour
_verify_ip, _request_ip = RateLimiter(30, 3600), RateLimiter(5, 3600)


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "")


def _fail(e: trial.TrialError) -> HTTPException:
    return HTTPException(e.status, str(e))


def _admin(member: OrgMember, db: Session) -> None:
    if db.get(Organization, member.organization_id).billing_status != "exempt":
        raise HTTPException(403, "Trials are managed by the BrandsLens team.")


# ---------------------------------------------------------------- public
class StartBody(BaseModel):
    email: str
    name: str
    company: str
    job_title: str = ""
    location: str = ""
    marketing_consent: bool = False
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    utm_term: str = ""
    utm_content: str = ""
    session_id: str = ""


class VerifyBody(BaseModel):
    email: str
    code: str
    password: str


class AccessBody(BaseModel):
    email: str
    name: str
    company: str
    job_title: str = ""
    location: str = ""
    note: str = ""


@router.get("/api/public/trial/status")
def trial_status(response: Response, db: Session = Depends(get_db)) -> dict:
    """Is a trial on offer right now, and what does it include? The sites use this to show or hide the trial button."""
    response.headers["Cache-Control"] = "public, max-age=30"
    ok, reason = trial.accepting(db)
    s = trial.get_settings(db)
    return {"open": ok, "reason": "" if ok else reason, "days": s["days"], "keywords": s["caps"]["keywords"], "workspaces": s["caps"]["workspaces"]}


@router.post("/api/public/trial/start")
def trial_start(body: StartBody, request: Request, db: Session = Depends(get_db)) -> dict:
    email = trial.normalise_email(body.email)
    if not _start_ip.allow(_ip(request)) or not _start_email.allow(email):
        raise HTTPException(429, "Too many attempts. Please try again later.")
    try:
        row, code = trial.start(db, email=email, name=body.name, company=body.company, job_title=body.job_title, location=body.location, consent=body.marketing_consent,
                                utm={"source": body.utm_source, "medium": body.utm_medium, "campaign": body.utm_campaign, "term": body.utm_term,
                                     "content": body.utm_content}, session_id=body.session_id, ip_hash=trial.hash_ip(_ip(request)))
    except trial.TrialError as e:
        raise _fail(e)
    trial.send_code_email(row, code)
    return {"sent": True, "email": row.email, "expires_minutes": trial.CODE_MINUTES}


@router.post("/api/public/trial/verify")
def trial_verify(body: VerifyBody, request: Request, db: Session = Depends(get_db)) -> dict:
    if not _verify_ip.allow(_ip(request)):
        raise HTTPException(429, "Too many attempts. Please try again later.")
    try:
        owner, token, row = trial.verify(db, email=body.email, code=body.code, password=body.password)
    except trial.TrialError as e:
        raise _fail(e)
    from .main import _member_dict
    workspaces = db.scalars(select(Workspace).where(Workspace.organization_id == owner.organization_id)).all()
    return {"token": token, "member": _member_dict(owner), "workspace_ids": [w.id for w in workspaces], "trial_ends_at": row.ends_at.isoformat()}


@router.post("/api/public/trial/request-access")
def trial_request_access(body: AccessBody, request: Request, db: Session = Depends(get_db)) -> dict:
    if not _request_ip.allow(_ip(request)):
        raise HTTPException(429, "Too many requests. Please try again later.")
    try:
        trial.request_access(db, email=body.email, name=body.name, company=body.company, note=body.note, job_title=body.job_title, location=body.location)
    except trial.TrialError as e:
        raise _fail(e)
    return {"received": True}


@router.get("/api/public/trial/unsubscribe", response_class=HTMLResponse)
def trial_unsubscribe(token: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    done = trial.unsubscribe(db, token)
    msg = ("You're unsubscribed from BrandsLens tips and offers. You will still get important messages about your trial."
           if done else "That link isn't valid. If you'd like to stop emails, reply to any message and we'll do it for you.")
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>BrandsLens</title>"
                        f"<body style='font-family:system-ui,sans-serif;max-width:480px;margin:15vh auto;padding:0 20px;color:#101935'>"
                        f"<h2>BrandsLens</h2><p>{msg}</p></body>")


# ---------------------------------------------------------------- the signed-in person (for the in-app banner)
@router.get("/api/trial/me")
def trial_me(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    return trial.trial_state(db, db.get(Organization, member.organization_id))


# ---------------------------------------------------------------- Super Admin
class SettingsBody(BaseModel):
    enabled: bool | None = None
    days: int | None = None
    daily_cap: int | None = None
    stop_date: str | None = None
    offer_code: str | None = None
    caps: dict | None = None


class ExtendBody(BaseModel):
    days: int


class SetEndBody(BaseModel):
    ends_at: datetime


class BulkBody(BaseModel):
    days: int | None = None
    until: datetime | None = None


class StepsBody(BaseModel):
    steps: list[dict]


class TestBody(BaseModel):
    step_key: str
    to: str


def _row(db: Session, signup_id: str) -> TrialSignup:
    row = db.get(TrialSignup, signup_id)
    if not row:
        raise HTTPException(404, "Lead not found.")
    return row


def _done(db: Session, row: TrialSignup) -> dict:
    return trial.list_signups(db, q=row.email, limit_=1)["rows"][0]


@router.get("/api/admin/trials/settings")
def admin_trial_settings(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    _admin(member, db)
    return {"settings": trial.get_settings(db), "summary": trial.summary(db), "bounds": trial.CAP_BOUNDS}


@router.put("/api/admin/trials/settings")
def admin_trial_save_settings(body: SettingsBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    _admin(member, db)
    try:
        return {"settings": trial.save_settings(db, member.email, body.model_dump(exclude_unset=True))}
    except trial.TrialError as e:
        raise _fail(e)


@router.get("/api/admin/trials")
def admin_trial_list(status: str = "", q: str = "", limit: int = 50, offset: int = 0, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    _admin(member, db)
    return {**trial.list_signups(db, status=status, q=q, limit_=limit, offset=offset), "summary": trial.summary(db)}


@router.get("/api/admin/trials/export.csv")
def admin_trial_export(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> Response:
    _admin(member, db)
    return Response(trial.export_csv(db), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="brandslens-trial-leads.csv"', "Cache-Control": "no-store"})


def _act(db: Session, member: OrgMember, signup_id: str, fn, *args) -> dict:
    _admin(member, db)
    row = _row(db, signup_id)
    try:
        fn(db, row, *args)
    except trial.TrialError as e:
        raise _fail(e)
    return {"lead": _done(db, row)}


@router.post("/api/admin/trials/{signup_id}/extend")
def admin_trial_extend(signup_id: str, body: ExtendBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    return _act(db, member, signup_id, trial.extend, body.days)


@router.post("/api/admin/trials/{signup_id}/set-end")
def admin_trial_set_end(signup_id: str, body: SetEndBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    return _act(db, member, signup_id, trial.set_end, body.ends_at)


@router.post("/api/admin/trials/{signup_id}/end")
def admin_trial_end(signup_id: str, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    return _act(db, member, signup_id, trial.end_now)


@router.post("/api/admin/trials/{signup_id}/block")
def admin_trial_block(signup_id: str, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    return _act(db, member, signup_id, trial.block)


@router.post("/api/admin/trials/{signup_id}/unblock")
def admin_trial_unblock(signup_id: str, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    return _act(db, member, signup_id, trial.unblock)


@router.post("/api/admin/trials/{signup_id}/approve")
def admin_trial_approve(signup_id: str, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    return _act(db, member, signup_id, trial.approve)


@router.post("/api/admin/trials/bulk-extend")
def admin_trial_bulk_extend(body: BulkBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    _admin(member, db)
    try:
        return {"extended": trial.bulk_extend(db, days=body.days, until=body.until)}
    except trial.TrialError as e:
        raise _fail(e)


@router.get("/api/admin/trials/nurture")
def admin_trial_nurture(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    _admin(member, db)
    return {"steps": trial.steps(db), "variables": ["first_name", "company", "ends_on", "days_left", "app_url", "upgrade_url", "offer_line", "unsubscribe_url"]}


@router.put("/api/admin/trials/nurture")
def admin_trial_nurture_save(body: StepsBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    _admin(member, db)
    try:
        return {"steps": trial.save_steps(db, member.email, body.steps)}
    except trial.TrialError as e:
        raise _fail(e)


@router.post("/api/admin/trials/nurture/test")
def admin_trial_nurture_test(body: TestBody, member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    _admin(member, db)
    try:
        return {"sent": trial.send_test(db, body.step_key, body.to)}
    except trial.TrialError as e:
        raise _fail(e)


@router.post("/api/admin/trials/run-lifecycle")
def admin_trial_run(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> dict:
    """Runs the expiry and email pass now, instead of waiting for the next scheduled run."""
    _admin(member, db)
    return trial.run_lifecycle(db)
