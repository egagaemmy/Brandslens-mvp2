"""routes_referrals.py — everything HTTP about referrals, kept out of
main.py (which is already very large). Three audiences:

  * the public — anyone can join as a referrer, and a referrer with their
    private portal link can see their own earnings (no login, no
    BrandsLens account needed);
  * a logged-in subscriber, who gets a link automatically;
  * the platform admin, who sets the rate and pays the commissions out.
"""
from __future__ import annotations
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .deps import active_member
from .models import OrgMember, Organization, Referrer, ReferralCommission, now_utc, aware
from .services import referrals
from .services.mailer import send_referrer_welcome_email
from .services.settings import set_setting

router = APIRouter()


def _require_admin(member: OrgMember, db: Session) -> None:
    org = db.get(Organization, member.organization_id)
    if org.billing_status != "exempt":
        raise HTTPException(403, "Referral management is restricted to the BrandsLens team.")


def _by_portal_token(db: Session, token: str) -> Referrer:
    ref = db.scalar(select(Referrer).where(Referrer.portal_token == token))
    if not ref:
        raise HTTPException(404, "This link isn't valid. Check your email for your latest one.")
    return ref


def _email_welcome(db: Session, ref: Referrer) -> None:
    send_referrer_welcome_email(ref.name, ref.email, ref.code, referrals.referral_link(ref.code),
                                referrals.portal_url(ref.portal_token), referrals.get_rate(db))
    ref.last_emailed_at = now_utc()
    db.commit()


# ---------------------------------------------------------------- public

class JoinBody(BaseModel):
    name: str
    email: str
    kind: str = "individual"
    company: str = ""


@router.post("/api/referrals/join")
def join_as_referrer(body: JoinBody, db: Session = Depends(get_db)) -> dict:
    """Public. The response is identical whether the email is new or
    already registered, and never contains the portal link — that goes
    to the inbox only. Otherwise anyone could type a stranger's email
    and read their earnings page."""
    name, email = body.name.strip(), body.email.strip().lower()
    if not name or "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(422, "Please enter your name and a valid email address.")
    if body.kind == "corporate" and not body.company.strip():
        raise HTTPException(422, "Please enter your company name.")
    ref, _created = referrals.create_referrer(db, name, email, body.kind, body.company, source="self")
    throttled = ref.last_emailed_at and (now_utc() - aware(ref.last_emailed_at)) < referrals.EMAIL_THROTTLE
    if not throttled:
        _email_welcome(db, ref)
    return {"ok": True, "message": "Check your inbox — we've emailed your personal referral link and your private earnings page."}


@router.get("/api/referrals/portal/{token}")
def portal_summary(token: str, db: Session = Depends(get_db)) -> dict:
    return referrals.referrer_summary(db, _by_portal_token(db, token))


class PayoutBody(BaseModel):
    payout_details: str


@router.put("/api/referrals/portal/{token}/payout")
def portal_set_payout(token: str, body: PayoutBody, db: Session = Depends(get_db)) -> dict:
    ref = _by_portal_token(db, token)
    ref.payout_details = body.payout_details.strip()[:2000]
    db.commit()
    return {"ok": True}


# ------------------------------------------------------ logged-in member

@router.get("/api/me/referral")
def my_referral(member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    """Every subscriber gets a link automatically — created the first time
    they open the Refer & Earn screen. Returns the portal token so the app
    can show the same earnings view an outside agent sees."""
    ref, created = referrals.create_referrer(db, member.name or member.email, member.email, "individual",
                                             "", source="member", member_id=member.id)
    return {"portal_token": ref.portal_token, "code": ref.code, "referral_link": referrals.referral_link(ref.code)}


# ----------------------------------------------------------------- admin

class AgentBody(BaseModel):
    name: str
    email: str
    kind: str = "individual"
    company: str = ""


class SettingsBody(BaseModel):
    commission_rate: float
    hold_days: int


class VoidBody(BaseModel):
    note: str = ""


class StatusBody(BaseModel):
    status: str


@router.get("/api/admin/referrals")
def admin_overview(member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_admin(member, db)
    referrals.sweep_approvals(db)
    refs = db.scalars(select(Referrer).order_by(Referrer.created_at.desc())).all()
    all_comms = db.scalars(select(ReferralCommission)).all()
    by_ref: dict[str, list[ReferralCommission]] = {}
    for c in all_comms:
        by_ref.setdefault(c.referrer_id, []).append(c)
    return {
        "commission_rate": referrals.get_rate(db), "hold_days": referrals.get_hold_days(db),
        "referrers": [referrals.admin_referrer_row(db, r, by_ref.get(r.id, [])) for r in refs],
    }


@router.get("/api/admin/referrals/commissions")
def admin_commissions(status: str | None = None, member: OrgMember = Depends(active_member),
                      db: Session = Depends(get_db)) -> list[dict]:
    _require_admin(member, db)
    referrals.sweep_approvals(db)
    q = select(ReferralCommission).order_by(ReferralCommission.created_at.desc())
    if status:
        q = q.where(ReferralCommission.status == status)
    out = []
    for c in db.scalars(q).all():
        ref = db.get(Referrer, c.referrer_id)
        out.append(referrals.commission_row(c, ref))
    return out


@router.post("/api/admin/referrals/agents")
def admin_create_agent(body: AgentBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    """Creates a referrer on someone's behalf — typically a freelance sales
    agent you've recruited, who has no BrandsLens account. The link is
    generated instantly and returned here so you can send it yourself, and
    they're emailed too."""
    _require_admin(member, db)
    name, email = body.name.strip(), body.email.strip().lower()
    if not name or "@" not in email:
        raise HTTPException(422, "Name and a valid email are required.")
    ref, created = referrals.create_referrer(db, name, email, body.kind, body.company, source="admin")
    if not created:
        raise HTTPException(409, f"{email} is already registered (code {ref.code}).")
    _email_welcome(db, ref)
    return {"code": ref.code, "referral_link": referrals.referral_link(ref.code),
            "portal_url": referrals.portal_url(ref.portal_token)}


@router.put("/api/admin/referrals/settings")
def admin_update_settings(body: SettingsBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_admin(member, db)
    if not (0 < body.commission_rate <= 50):
        raise HTTPException(422, "Commission rate must be above 0 and no more than 50 percent.")
    if not (0 <= body.hold_days <= 365):
        raise HTTPException(422, "Hold period must be between 0 and 365 days.")
    set_setting(db, referrals.RATE_KEY, body.commission_rate, updated_by=member.email)
    set_setting(db, referrals.HOLD_KEY, body.hold_days, updated_by=member.email)
    return {"ok": True}


@router.post("/api/admin/referrals/commissions/{commission_id}/pay")
def admin_mark_paid(commission_id: str, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_admin(member, db)
    referrals.sweep_approvals(db)
    c = db.get(ReferralCommission, commission_id)
    if not c:
        raise HTTPException(404, "Commission not found.")
    if c.status == "pending":
        raise HTTPException(409, f"Still in the hold period until {aware(c.approvable_at):%B %d, %Y}.")
    if c.status != "approved":
        raise HTTPException(409, f"This commission is already {c.status}.")
    c.status, c.paid_at = "paid", now_utc()
    db.commit()
    return {"ok": True}


@router.post("/api/admin/referrals/commissions/{commission_id}/void")
def admin_void(commission_id: str, body: VoidBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_admin(member, db)
    c = db.get(ReferralCommission, commission_id)
    if not c:
        raise HTTPException(404, "Commission not found.")
    if c.status == "paid":
        raise HTTPException(409, "This commission has already been paid out and can't be voided here.")
    c.status, c.note = "void", body.note.strip()[:500]
    db.commit()
    return {"ok": True}


@router.patch("/api/admin/referrals/referrers/{referrer_id}")
def admin_set_status(referrer_id: str, body: StatusBody, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    _require_admin(member, db)
    if body.status not in ("active", "disabled"):
        raise HTTPException(422, "Status must be 'active' or 'disabled'.")
    ref = db.get(Referrer, referrer_id)
    if not ref:
        raise HTTPException(404, "Referrer not found.")
    ref.status = body.status
    db.commit()
    return {"ok": True}


@router.post("/api/admin/referrals/referrers/{referrer_id}/rotate-token")
def admin_rotate_token(referrer_id: str, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> dict:
    """If a private portal link ever leaks, this kills the old one."""
    _require_admin(member, db)
    ref = db.get(Referrer, referrer_id)
    if not ref:
        raise HTTPException(404, "Referrer not found.")
    ref.portal_token = secrets.token_urlsafe(32)
    db.commit()
    return {"portal_url": referrals.portal_url(ref.portal_token)}
