"""referrals.py — referral links and commissions.

The rules, in one place:
  * A commission exists only for a referred customer's FIRST payment,
    and only once that payment is verified. No payment, no commission.
  * It's a percentage of what the customer actually paid, in the
    currency they actually paid in (USD or NGN), snapshotted at the
    moment it's created.
  * It starts 'pending' for a hold period (refund/chargeback cover),
    then becomes 'approved', then an admin marks it 'paid' after sending
    the money. An admin can void it at any point before that.
  * Nobody earns on their own purchase.
"""
from __future__ import annotations
import logging
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import MARKETING_URL, APP_URL
from ..models import Referrer, ReferralCommission, now_utc, aware
from .settings import get_setting

log = logging.getLogger("referrals")

DEFAULT_RATE_PERCENT = 3.0
DEFAULT_HOLD_DAYS = 30
RATE_KEY = "referral:commission_rate"
HOLD_KEY = "referral:hold_days"

# No 0/O or 1/I/L — codes get read aloud and typed by hand.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
EMAIL_THROTTLE = timedelta(minutes=10)


def get_rate(db: Session) -> float:
    return float(get_setting(db, RATE_KEY, DEFAULT_RATE_PERCENT))


def get_hold_days(db: Session) -> int:
    return int(get_setting(db, HOLD_KEY, DEFAULT_HOLD_DAYS))


def normalize_code(code: str | None) -> str:
    return (code or "").strip().upper()


def referral_link(code: str) -> str:
    return f"{MARKETING_URL}/?ref={code}"


def portal_url(token: str) -> str:
    return f"{APP_URL}/agents/portal/{token}"


def _new_code(db: Session, name: str) -> str:
    prefix = "".join(ch for ch in name.upper() if ch.isalpha())[:4] or "BL"
    for _ in range(20):
        code = f"{prefix}-{''.join(secrets.choice(_CODE_ALPHABET) for _ in range(4))}"
        if not db.scalar(select(Referrer).where(Referrer.code == code)):
            return code
    # Astronomically unlikely, but never loop forever: lengthen instead.
    return f"{prefix}-{''.join(secrets.choice(_CODE_ALPHABET) for _ in range(8))}"


def create_referrer(db: Session, name: str, email: str, kind: str = "individual", company: str = "",
                    source: str = "self", member_id: str | None = None) -> tuple[Referrer, bool]:
    """Returns (referrer, created). One referrer per email — asking again
    returns the existing one rather than minting a second code."""
    email = email.strip().lower()
    existing = db.scalar(select(Referrer).where(Referrer.email == email))
    if existing:
        if member_id and not existing.member_id:
            existing.member_id = member_id
            db.commit()
        return existing, False
    ref = Referrer(
        code=_new_code(db, name), name=name.strip(), email=email,
        kind=kind if kind in ("individual", "corporate") else "individual",
        company=company.strip(), source=source, member_id=member_id,
        portal_token=secrets.token_urlsafe(32),
    )
    db.add(ref)
    db.commit()
    return ref, True


def find_active_by_code(db: Session, code: str | None) -> Referrer | None:
    code = normalize_code(code)
    if not code:
        return None
    ref = db.scalar(select(Referrer).where(Referrer.code == code))
    return ref if ref and ref.status == "active" else None


def record_first_payment_commission(db: Session, organization_id: str, organization_name: str,
                                    buyer_email: str, code: str | None, amount_paid: float,
                                    currency: str, payment_ref: str) -> ReferralCommission | None:
    """Called only from the paths that create an account from a verified
    first payment. Every guard here returns None quietly rather than
    raising — a referral problem must never get in the way of a customer
    who has genuinely paid."""
    ref = find_active_by_code(db, code)
    if not ref:
        return None
    if buyer_email.strip().lower() == ref.email.lower():
        log.info("Referral %s ignored: referrer and buyer are the same person (%s)", ref.code, buyer_email)
        return None
    try:
        amount = float(amount_paid)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    if db.scalar(select(ReferralCommission).where(ReferralCommission.organization_id == organization_id)):
        return None  # first payment only — this organization already has its one commission

    rate = get_rate(db)
    now = now_utc()
    commission = ReferralCommission(
        referrer_id=ref.id, organization_id=organization_id, organization_name=organization_name,
        payment_ref=payment_ref, amount_paid=round(amount, 2), currency=(currency or "USD").upper()[:3],
        rate_percent=rate, commission=round(amount * rate / 100, 2),
        status="pending", approvable_at=now + timedelta(days=get_hold_days(db)),
    )
    db.add(commission)
    db.commit()
    log.info("Commission recorded: referrer=%s org=%s %s %s at %s%%", ref.code, organization_id,
             commission.currency, commission.commission, rate)
    return commission


def sweep_approvals(db: Session) -> int:
    """pending -> approved, once the hold period has passed. Cheap enough
    to run whenever commissions are read, so there's no separate job to
    keep alive and no window where the screen disagrees with reality."""
    now = now_utc()
    due = db.scalars(select(ReferralCommission).where(ReferralCommission.status == "pending")).all()
    changed = 0
    for c in due:
        if aware(c.approvable_at) <= now:
            c.status = "approved"
            changed += 1
    if changed:
        db.commit()
    return changed


def _totals(commissions: list[ReferralCommission]) -> dict[str, dict[str, float]]:
    """{currency: {pending, approved, paid}} — never mixes currencies,
    and void commissions are left out entirely."""
    out: dict[str, dict[str, float]] = {}
    for c in commissions:
        if c.status == "void":
            continue
        bucket = out.setdefault(c.currency, {"pending": 0.0, "approved": 0.0, "paid": 0.0})
        bucket[c.status] = round(bucket[c.status] + c.commission, 2)
    return out


def referrer_summary(db: Session, ref: Referrer) -> dict:
    sweep_approvals(db)
    commissions = db.scalars(select(ReferralCommission).where(ReferralCommission.referrer_id == ref.id)
                             .order_by(ReferralCommission.created_at.desc())).all()
    return {
        "name": ref.name, "code": ref.code, "kind": ref.kind, "status": ref.status,
        "referral_link": referral_link(ref.code),
        "commission_rate": get_rate(db), "hold_days": get_hold_days(db),
        "payout_details": ref.payout_details,
        "sales_count": sum(1 for c in commissions if c.status != "void"),
        "totals": _totals(commissions),
        # Deliberately no customer names here: the referrer needs to know
        # what they earned and when, not who the customer is.
        "commissions": [{
            "date": c.created_at.isoformat(), "amount_paid": c.amount_paid, "currency": c.currency,
            "commission": c.commission, "status": c.status, "approvable_at": c.approvable_at.isoformat(),
        } for c in commissions],
    }


def admin_referrer_row(db: Session, ref: Referrer, commissions: list[ReferralCommission]) -> dict:
    return {
        "id": ref.id, "code": ref.code, "name": ref.name, "email": ref.email, "kind": ref.kind,
        "company": ref.company, "source": ref.source, "status": ref.status,
        "created_at": ref.created_at.isoformat(), "payout_details": ref.payout_details,
        "referral_link": referral_link(ref.code), "portal_url": portal_url(ref.portal_token),
        "sales_count": sum(1 for c in commissions if c.status != "void"),
        "totals": _totals(commissions),
    }


def commission_row(c: ReferralCommission, ref: Referrer) -> dict:
    return {
        "id": c.id, "referrer_id": ref.id, "referrer_name": ref.name, "referrer_code": ref.code,
        "referrer_payout_details": ref.payout_details, "organization_name": c.organization_name,
        "amount_paid": c.amount_paid, "currency": c.currency, "rate_percent": c.rate_percent,
        "commission": c.commission, "status": c.status, "note": c.note,
        "created_at": c.created_at.isoformat(), "approvable_at": c.approvable_at.isoformat(),
        "paid_at": c.paid_at.isoformat() if c.paid_at else None,
    }
