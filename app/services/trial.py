"""services/trial.py. The free trial: a business-email lead gets a real, time-limited, capped workspace, and the team gets the lead.

HOW IT FITS
  * A trial is an ordinary Organization whose billing_status is 'trialing' and whose trial_ends_at is an exact UTC moment, fixed the
    day it starts. The access gate (deps.active_member) checks that moment on EVERY request, so access stops exactly then.
  * Workspace, keyword, team, competitor and history limits are small overrides (limit()) read by the same checks that already guard
    paid plans. AI drafts and Ask BrandsLens questions are counted (consume()) because each one costs real AI credit.
  * Paying is untouched: a trial user who buys goes through the normal checkout, and _activate_plan flips them to a paying customer.
  * Everything the team can tune (on/off, length, daily cap, stop date, caps) lives in AppSetting, so it changes with no redeploy.
    The length applies to NEW trials only: each trial keeps the end time it was promised.

THE LEAD
  TrialSignup is the lead record. It exists before the email is verified and stays after the trial, so it is the list the team
  nurtures and exports. Marketing emails go only to people who ticked the box and have not unsubscribed. The emails about the
  trial itself (code, welcome, ending, ended) go to everyone, because they are part of the service.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import APP_URL, LEAD_NOTIFY_EMAIL, PUBLIC_API_URL, TRIAL_FROM, TRIAL_REPLY_TO
from ..models import Organization, OrgMember, TrialEmail, TrialSignup, Workspace, aware, now_utc
from .promos import clean_tag
from .settings import get_setting, set_setting

log = logging.getLogger("trial")

CODE_MINUTES = 15
MAX_ATTEMPTS = 5
RESEND_SECONDS = 60
DOMAIN_MONTHS = 12
K = {"enabled": "trial:enabled", "days": "trial:days", "stop_date": "trial:stop_date", "daily_cap": "trial:daily_cap",
     "caps": "trial:caps", "offer_code": "trial:offer_code", "steps": "trial:nurture"}


class TrialError(Exception):
    """Something to tell the person, with the HTTP status that fits."""
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class TrialExpired(Exception):
    pass


# ------------------------------------------------------------------ who may start a trial: business emails only
FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "rocketmail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "protonmail.ch", "pm.me", "gmx.com", "gmx.net", "gmx.de", "mail.com",
    "zohomail.com", "zoho.com", "zoho.eu", "zohomail.eu", "zohomail.in", "yandex.com", "yandex.ru", "mail.ru", "inbox.com", "fastmail.com", "hey.com", "tutanota.com", "tuta.io", "tutamail.com",
    "qq.com", "163.com", "126.com", "sina.com", "naver.com", "daum.net", "web.de", "t-online.de", "orange.fr", "free.fr", "libero.it",
    "seznam.cz", "rediffmail.com", "lycos.com", "bigpond.com", "att.net", "verizon.net", "comcast.net", "sbcglobal.net", "btinternet.com",
}
# Country versions of the big free providers (yahoo.co.uk, hotmail.fr, outlook.de ...). Deliberately NOT a loose prefix match: that would also
# turn away real companies such as livestock.com or outlookconsulting.com.
_FREE_PATTERNS = (re.compile(r"^(yahoo|outlook|hotmail|live|msn|gmx|yandex)\.(com?\.[a-z]{2}|[a-z]{2,3})$"),)
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "10minutemail.com", "tempmail.com", "temp-mail.org", "yopmail.com", "trashmail.com",
    "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc", "throwawaymail.com", "fakeinbox.com", "mintemail.com", "mohmal.com",
    "emailondeck.com", "tempinbox.com", "burnermail.io", "spamgourmet.com", "mailnesia.com", "tempr.email", "moakt.com", "mytemp.email",
}
_DISPOSABLE_PATTERN = re.compile(r"(^|\.)(mailinator|guerrillamail|10minutemail|tempmail|temp-mail|yopmail|trashmail|throwaway|fakeinbox)[a-z0-9-]*\.")
_EMAIL = re.compile(r"^[^@\s,;<>()\[\]\\\"]{1,64}@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def domain_of(email: str) -> str:
    return normalise_email(email).rpartition("@")[2]


def _parents(domain: str):
    labels = domain.split(".")
    for i in range(len(labels) - 1):
        yield ".".join(labels[i:])


def is_business_email(email: str) -> tuple[bool, str]:
    """(ok, reason). A real mailbox on a company domain, not a personal or throwaway address. This is friction, not proof of
    employment: the emailed code proves the person controls the mailbox, which is the part that matters."""
    email = normalise_email(email)
    if len(email) > 254 or not _EMAIL.match(email):
        return False, "Enter a valid email address."
    domain = domain_of(email)
    for d in _parents(domain):
        if d in DISPOSABLE_DOMAINS or _DISPOSABLE_PATTERN.search(d):
            return False, "Temporary email addresses can't be used for a trial. Please use your work email."
        if d in FREE_DOMAINS or any(p.match(d) for p in _FREE_PATTERNS):
            return False, ("Please use your work email address, for example name@yourcompany.com. "
                           "Personal addresses such as Gmail or Yahoo can't start a free trial.")
    return True, ""


# ------------------------------------------------------------------ settings (all editable in Super Admin, no redeploy)
DEFAULT_CAPS = {"workspaces": 1, "keywords": 5, "members": 3, "competitors": 3, "historical_days": 30, "ai_drafts": 10, "ask_messages": 25}
CAP_BOUNDS = {"workspaces": (1, 3), "keywords": (1, 50), "members": (1, 25), "competitors": (0, 8), "historical_days": (0, 365), "ai_drafts": (0, 200), "ask_messages": (0, 500)}


def _int(value, default: int, low: int, high: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, v))


def caps(db: Session) -> dict:
    saved = get_setting(db, K["caps"], {}) or {}
    return {k: _int(saved.get(k, d), d, *CAP_BOUNDS[k]) for k, d in DEFAULT_CAPS.items()}


def get_settings(db: Session) -> dict:
    return {"enabled": bool(get_setting(db, K["enabled"], False)),                      # off until the team switches it on
            "days": _int(get_setting(db, K["days"], 30), 30, 1, 180),
            "stop_date": str(get_setting(db, K["stop_date"], "") or ""),
            "daily_cap": _int(get_setting(db, K["daily_cap"], 50), 50, 0, 1000),
            "offer_code": str(get_setting(db, K["offer_code"], "") or ""),
            "caps": caps(db)}


def save_settings(db: Session, actor: str, data: dict) -> dict:
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise TrialError("enabled must be true or false.")
        set_setting(db, K["enabled"], data["enabled"], updated_by=actor)
    if "days" in data:
        if not isinstance(data["days"], int) or isinstance(data["days"], bool) or not 1 <= data["days"] <= 180:
            raise TrialError("The trial length must be a whole number of days from 1 to 180.")
        set_setting(db, K["days"], data["days"], updated_by=actor)
    if "daily_cap" in data:
        if not isinstance(data["daily_cap"], int) or isinstance(data["daily_cap"], bool) or not 0 <= data["daily_cap"] <= 1000:
            raise TrialError("The daily limit must be a whole number from 0 to 1000.")
        set_setting(db, K["daily_cap"], data["daily_cap"], updated_by=actor)
    if "stop_date" in data:
        v = (data["stop_date"] or "").strip()
        if v:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                raise TrialError("The stop date must look like 2026-12-31, or be left empty.")
        set_setting(db, K["stop_date"], v, updated_by=actor)
    if "offer_code" in data:
        set_setting(db, K["offer_code"], re.sub(r"[^A-Za-z0-9_-]", "", str(data["offer_code"] or ""))[:30].upper(), updated_by=actor)
    if "caps" in data:
        current = caps(db)
        for k, v in (data["caps"] or {}).items():
            if k not in DEFAULT_CAPS:
                raise TrialError(f"Unknown limit: {k}.")
            low, high = CAP_BOUNDS[k]
            if not isinstance(v, int) or isinstance(v, bool) or not low <= v <= high:
                raise TrialError(f"The {k.replace('_', ' ')} limit must be a whole number from {low} to {high}.")
            current[k] = v
        set_setting(db, K["caps"], current, updated_by=actor)
    db.commit()
    return get_settings(db)


def _now(now: datetime | None = None) -> datetime:
    """The moment to work with, always timezone-aware UTC. Some databases hand back naive datetimes, and comparing naive with aware raises."""
    return aware(now) if now else now_utc()


def _midnight(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def started_today(db: Session, now: datetime) -> int:
    return db.scalar(select(func.count(TrialSignup.id)).where(TrialSignup.started_at >= _midnight(now))) or 0


def accepting(db: Session, now: datetime | None = None) -> tuple[bool, str]:
    now = _now(now)
    s = get_settings(db)
    if not s["enabled"]:
        return False, "Free trials aren't open right now."
    if s["stop_date"] and now.astimezone(timezone.utc).date() > datetime.strptime(s["stop_date"], "%Y-%m-%d").date():
        return False, "The free trial offer has closed."
    if started_today(db, now) >= s["daily_cap"]:
        return False, "We've reached today's limit for new trials. Please try again tomorrow."
    return True, ""


def is_trial(org: Organization) -> bool:
    return org.billing_status == "trialing"


def limit(db: Session, org: Organization, key: str, plan_default: int) -> int:
    """The limit that applies to this organization: the trial cap during a trial, the plan's own limit otherwise."""
    return caps(db)[key] if is_trial(org) else plan_default


def consume(db: Session, org: Organization, key: str) -> None:
    """Counts one use of something that costs AI credit, and refuses once the trial allowance is spent."""
    if not is_trial(org):
        return
    attr = {"ai_drafts": "trial_ai_drafts", "ask_messages": "trial_ask_messages"}[key]
    used, cap = getattr(org, attr) or 0, caps(db)[key]
    label = {"ai_drafts": "AI statement drafts", "ask_messages": "Ask BrandsLens questions"}[key]
    if used >= cap:
        raise HTTPException(422, f"Your free trial includes {cap} {label}, and you've used them all. Choose a plan to keep going.")
    setattr(org, attr, used + 1)
    db.commit()


# ------------------------------------------------------------------ the exact clock and the gate
def days_left(ends: datetime | None, now: datetime | None = None) -> int:
    if ends is None:
        return 0
    seconds = (aware(ends) - (now or now_utc())).total_seconds()
    return max(0, int(-(-seconds // 86400)))


def _signup_for(db: Session, org_id: str) -> TrialSignup | None:
    return db.scalar(select(TrialSignup).where(TrialSignup.organization_id == org_id))


def expire_org(db: Session, org: Organization) -> None:
    if org.billing_status == "trialing":
        org.billing_status = "unpaid"
    row = _signup_for(db, org.id)
    if row and row.status == "active":
        row.status = "expired"
    db.commit()


def check_active(db: Session, org: Organization) -> None:
    """Called by the access gate for every request from a trialing organization. Access ends AT the stored moment, not at the next
    nightly job. A 'trialing' organization with no end date at all is treated as expired: it can never be open-ended by accident."""
    ends = aware(org.trial_ends_at)
    if ends is None or ends <= now_utc():
        expire_org(db, org)
        raise TrialExpired()


def trial_state(db: Session, org: Organization) -> dict:
    if not is_trial(org):
        row = _signup_for(db, org.id)
        if row and row.status == "expired":          # lets the paywall say "your free trial has ended" instead of a bare "payment required"
            return {"is_trial": False, "trial_expired": True, "ended_at": row.ends_at.isoformat() if row.ends_at else None}
        return {"is_trial": False}
    c = caps(db)
    return {"is_trial": True, "started_at": org.trial_started_at.isoformat() if org.trial_started_at else None,
            "ends_at": org.trial_ends_at.isoformat() if org.trial_ends_at else None, "days_left": days_left(org.trial_ends_at),
            "limits": c, "usage": {"ai_drafts": org.trial_ai_drafts or 0, "ask_messages": org.trial_ask_messages or 0}}


def mark_converted(db: Session, org: Organization) -> None:
    """Called when a trial organization pays. Never allowed to break a payment."""
    try:
        row = _signup_for(db, org.id)
        if row and row.status in ("active", "expired"):
            row.status = "converted"
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("Could not mark trial %s as converted", org.id)


# ------------------------------------------------------------------ starting a trial
def _hash_code(signup_id: str, code: str) -> str:
    return hmac.new(signup_id.encode(), code.encode(), hashlib.sha256).hexdigest()


def hash_ip(ip: str) -> str:
    return hashlib.sha256(("trial|" + (ip or "")).encode()).hexdigest()


def _utm(utm: dict) -> dict:
    return {f"utm_{k}": clean_tag((utm or {}).get(k, ""), 80 if k in ("campaign", "term", "content") else 60) for k in ("source", "medium", "campaign", "term", "content")}


def start(db: Session, *, email: str, name: str, company: str, consent: bool, utm: dict | None = None,
          session_id: str = "", ip_hash: str = "", now: datetime | None = None) -> tuple[TrialSignup, str]:
    """Checks the rules and prepares the emailed code. Returns the lead row and the code (the caller sends it)."""
    now = _now(now)
    email, name, company = normalise_email(email), (name or "").strip(), (company or "").strip()
    ok, reason = accepting(db, now)
    if not ok:
        raise TrialError(reason, 503)
    if len(name) < 2:
        raise TrialError("Please enter your name.")
    if len(company) < 2:
        raise TrialError("Please enter your company name.")
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == email))
    approved = bool(row and row.status == "approved")
    if not approved:
        ok, why = is_business_email(email)
        if not ok:
            raise TrialError(why)
    if row and row.status == "blocked":
        raise TrialError("We couldn't start a trial for this address. Please contact info@brandslens.com.", 403)
    if db.scalar(select(OrgMember).where(OrgMember.email == email)):
        raise TrialError("An account with this email already exists. Please log in instead.", 409)
    if row and row.status in ("active", "expired", "converted"):
        raise TrialError("This email has already used a free trial. Log in, or choose a plan to continue.", 409)
    if not approved:
        domain = domain_of(email)
        other = db.scalar(select(TrialSignup).where(
            TrialSignup.domain == domain, TrialSignup.email != email, TrialSignup.status.in_(("active", "expired", "converted")),
            TrialSignup.started_at > now - timedelta(days=30 * DOMAIN_MONTHS)))
        if other:
            raise TrialError(f"Your company ({domain}) already has a BrandsLens trial. Ask the person who started it to invite you from Team settings.", 409)
    if row and row.code_expires_at and aware(row.code_expires_at) - timedelta(minutes=CODE_MINUTES) > now - timedelta(seconds=RESEND_SECONDS):
        raise TrialError("A code was just sent. Please wait a minute before asking for another.", 429)
    if not row:
        row = TrialSignup(email=email, domain=domain_of(email), status="pending")
        db.add(row)
        db.flush()
    code = f"{secrets.randbelow(1_000_000):06d}"
    row.name, row.company = name[:200], company[:200]
    if consent:
        row.marketing_consent, row.consent_at, row.unsubscribed_at = True, now, None
    for k, v in _utm(utm or {}).items():
        if v:
            setattr(row, k, v)
    row.session_id, row.ip_hash = (session_id or "")[:64], ip_hash
    row.code_hash, row.code_expires_at, row.code_attempts = _hash_code(row.id, code), now + timedelta(minutes=CODE_MINUTES), 0
    db.commit()
    return row, code


def send_code_email(row: TrialSignup, code: str) -> bool:
    from . import mailer
    inner = (f"<p>Hi {html.escape(row.name.split(' ')[0] or 'there')},</p><p>Your BrandsLens verification code is:</p>"
             f"<p style=\"font-size:28px;font-weight:800;letter-spacing:6px\">{code}</p>"
             f"<p>It works for {CODE_MINUTES} minutes. If you didn't ask for it, you can ignore this email.</p>")
    return mailer.send_email(row.email, "Your BrandsLens verification code", mailer._wrap(inner), from_addr=TRIAL_FROM, reply_to=TRIAL_REPLY_TO)


def verify(db: Session, *, email: str, code: str, password: str, now: datetime | None = None):
    """Checks the code and creates the trial account. Returns (owner, session token, lead row)."""
    from .auth import DEFAULT_RSS_FEEDS, _seed_default_threat_categories, hash_password, issue_session
    now = _now(now)
    email = normalise_email(email)
    if len(password or "") < 8:
        raise TrialError("Password must be at least 8 characters.")
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == email))
    if not row or row.status not in ("pending", "approved") or not row.code_hash:
        raise TrialError("That code isn't valid. Please request a new one.")
    if (row.code_attempts or 0) >= MAX_ATTEMPTS:
        raise TrialError("Too many attempts. Please request a new code.", 429)
    if not row.code_expires_at or aware(row.code_expires_at) < now:
        raise TrialError("That code has expired. Please request a new one.")
    row.code_attempts = (row.code_attempts or 0) + 1
    db.commit()
    if not hmac.compare_digest(row.code_hash, _hash_code(row.id, (code or "").strip())):
        raise TrialError("That code isn't right. Please check it and try again.")
    ok, reason = accepting(db, now)
    if not ok:
        raise TrialError(reason, 503)
    if db.scalar(select(OrgMember).where(OrgMember.email == email)):
        raise TrialError("An account with this email already exists. Please log in instead.", 409)

    settings, c = get_settings(db), caps(db)
    ends = now + timedelta(days=settings["days"])
    org = Organization(name=row.company, sector="", plan="professional", workspace_limit=c["workspaces"], keyword_limit=c["keywords"],
                       billing_status="trialing", trial_started_at=now, trial_ends_at=ends, trial_ai_drafts=0, trial_ask_messages=0)
    db.add(org)
    db.flush()
    owner = OrgMember(organization_id=org.id, email=email, name=row.name, password_hash=hash_password(password), role="owner",
                      status="active", activated_at=now)
    db.add(owner)
    db.flush()
    company = row.company
    starter = [f"{company} scam", f"{company} fraud", f"fake {company}", f"impersonating {company}", f"{company} refund"][: c["keywords"]]
    ws = Workspace(organization_id=org.id, name=company, sector="", owner_email=email,
                   brand_tokens=[t.strip().lower() for t in company.split() if len(t.strip()) > 2] or [company.lower()],
                   keywords=starter, rss_feeds=list(DEFAULT_RSS_FEEDS))
    db.add(ws)
    db.flush()
    _seed_default_threat_categories(db, ws)
    row.status, row.verified_at, row.started_at, row.ends_at, row.organization_id = "active", now, now, ends, org.id
    row.code_hash, row.code_expires_at = "", None
    db.commit()
    token = issue_session(db, owner)
    _notify_team_new_lead(row, ends)
    _forward_to_mailchimp(row)                          # only people who ticked the box
    run_nurture_for(db, row, now)                       # sends the welcome email straight away
    return owner, token, row


def _forward_to_mailchimp(row: TrialSignup) -> None:
    """Leads who agreed to marketing emails join the Mailchimp list the team already uses. Nobody else is ever sent there."""
    if not row.marketing_consent:
        return
    try:
        from . import mailer
        mailer.add_to_mailchimp(row.email, (row.name or "").strip().split(" ")[0])
    except Exception:  # noqa: BLE001 - Mailchimp being down must never break a sign-up
        log.exception("Could not add the trial lead to Mailchimp")


def _notify_team_new_lead(row: TrialSignup, ends: datetime) -> None:
    try:
        from . import mailer
        src = " / ".join(x for x in (row.utm_source, row.utm_medium, row.utm_campaign) if x) or "direct"
        inner = (f"<p><b>New free trial</b></p><p>{html.escape(row.name)}, {html.escape(row.company)} ({html.escape(row.domain)})<br>"
                 f"{html.escape(row.email)}<br>Source: {html.escape(src)}<br>Marketing emails: {'yes' if row.marketing_consent else 'no'}<br>"
                 f"Trial ends: {ends.strftime('%d %b %Y %H:%M')} UTC</p>")
        mailer.send_email(LEAD_NOTIFY_EMAIL, f"New trial: {row.company} ({row.domain})", mailer._wrap(inner), from_addr=TRIAL_FROM, reply_to=row.email)
    except Exception:  # noqa: BLE001 - telling the team must never break a sign-up
        log.exception("Could not notify the team of a new trial lead")


def request_access(db: Session, *, email: str, name: str, company: str, note: str) -> TrialSignup:
    """For someone with no business email. The team reviews the request and approves or ignores it."""
    email, name, company = normalise_email(email), (name or "").strip(), (company or "").strip()
    if len(name) < 2 or len(company) < 2:
        raise TrialError("Please enter your name and your company.")
    if not _EMAIL.match(email) or len(email) > 254:
        raise TrialError("Enter a valid email address.")
    if is_business_email(email)[0]:
        raise TrialError("You have a work email, so you can start your trial directly. No request is needed.")
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == email))
    if row and row.status == "blocked":
        raise TrialError("We couldn't process this request. Please contact info@brandslens.com.", 403)
    if db.scalar(select(OrgMember).where(OrgMember.email == email)):
        raise TrialError("An account with this email already exists. Please log in instead.", 409)
    if row and row.status in ("active", "expired", "converted"):
        raise TrialError("This email has already used a free trial.", 409)
    if not row:
        row = TrialSignup(email=email, domain=domain_of(email), status="requested")
        db.add(row)
    if row.status != "approved":
        row.status = "requested"
    row.name, row.company, row.note = name[:200], company[:200], (note or "").strip()[:500]
    db.commit()
    try:
        from . import mailer
        inner = (f"<p><b>Trial access request</b> (no business email)</p><p>{html.escape(name)}, {html.escape(company)}<br>{html.escape(email)}<br>"
                 f"{html.escape(row.note) or 'No note.'}</p><p>Approve or ignore it in Super Admin, Trials.</p>")
        mailer.send_email(LEAD_NOTIFY_EMAIL, f"Trial access request: {company}", mailer._wrap(inner), from_addr=TRIAL_FROM, reply_to=email)
    except Exception:  # noqa: BLE001
        log.exception("Could not notify the team of an access request")
    return row


# ------------------------------------------------------------------ team controls
def _org(db: Session, row: TrialSignup) -> Organization | None:
    return db.get(Organization, row.organization_id) if row.organization_id else None


def set_end(db: Session, row: TrialSignup, new_end: datetime, now: datetime | None = None) -> TrialSignup:
    now = _now(now)
    org = _org(db, row)
    if not org:
        raise TrialError("This person hasn't started a trial yet.")
    new_end = aware(new_end)
    row.ends_at, org.trial_ends_at = new_end, new_end
    never_paid = org.plan_activated_at is None and org.billing_status in ("unpaid", "trialing")
    if never_paid and row.status != "blocked":
        if new_end > now:
            org.billing_status, row.status = "trialing", "active"
        else:
            org.billing_status, row.status = "unpaid", "expired"
    db.commit()
    return row


def extend(db: Session, row: TrialSignup, days: int, now: datetime | None = None) -> TrialSignup:
    now = _now(now)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        raise TrialError("Extend by a whole number of days from 1 to 365.")
    return set_end(db, row, max(aware(row.ends_at) or now, now) + timedelta(days=days), now)


def end_now(db: Session, row: TrialSignup, now: datetime | None = None) -> TrialSignup:
    return set_end(db, row, now or now_utc(), now)


def block(db: Session, row: TrialSignup) -> TrialSignup:
    org = _org(db, row)
    if org and org.billing_status == "trialing":
        org.billing_status = "unpaid"
    row.status = "blocked"
    db.commit()
    return row


def unblock(db: Session, row: TrialSignup) -> TrialSignup:
    row.status = "expired" if row.organization_id else "pending"
    db.commit()
    return row


def approve(db: Session, row: TrialSignup) -> TrialSignup:
    if row.status != "requested":
        raise TrialError("Only a pending request can be approved.")
    row.status = "approved"
    db.commit()
    try:
        from . import mailer
        link = f"{APP_URL}/trial?email={row.email}"
        inner = (f"<p>Hi {html.escape(row.name.split(' ')[0] or 'there')},</p><p>Thanks for your interest. We've approved your free BrandsLens trial.</p>"
                 f"<p><a href=\"{html.escape(link)}\">Start your free trial</a></p><p>Just reply to this email if you have any questions.</p>")
        mailer.send_email(row.email, "Your BrandsLens trial is approved", mailer._wrap(inner), from_addr=TRIAL_FROM, reply_to=TRIAL_REPLY_TO)
    except Exception:  # noqa: BLE001
        log.exception("Could not send the approval email")
    return row


def bulk_extend(db: Session, *, days: int | None = None, until: datetime | None = None, now: datetime | None = None) -> int:
    """Lengthens every running trial. It can only ever EXTEND: shortening would break the end time each person was promised."""
    now = _now(now)
    if (days is None) == (until is None):
        raise TrialError("Give either a number of days or a date.")
    if days is not None and (not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365):
        raise TrialError("Extend by a whole number of days from 1 to 365.")
    count = 0
    for row in db.scalars(select(TrialSignup).where(TrialSignup.status == "active")).all():
        target = aware(row.ends_at) + timedelta(days=days) if days is not None else aware(until)
        if target > aware(row.ends_at):
            set_end(db, row, target, now)
            count += 1
    return count


def expire_due_trials(db: Session, now: datetime | None = None) -> int:
    now = _now(now)
    rows = db.scalars(select(Organization).where(Organization.billing_status == "trialing",
                                                 or_(Organization.trial_ends_at.is_(None), Organization.trial_ends_at <= now))).all()
    for org in rows:
        expire_org(db, org)
    return len(rows)


# ------------------------------------------------------------------ the team's lead list
def summary(db: Session, now: datetime | None = None) -> dict:
    now = _now(now)
    counts = dict(db.execute(select(TrialSignup.status, func.count(TrialSignup.id)).group_by(TrialSignup.status)).all())
    return {"counts": {s: counts.get(s, 0) for s in ("pending", "requested", "approved", "active", "expired", "converted", "blocked")},
            "total": sum(counts.values()), "started_today": started_today(db, now)}


def _row_dict(db: Session, row: TrialSignup, now: datetime) -> dict:
    now = _now(now)
    org = _org(db, row)
    ws = db.scalar(select(Workspace).where(Workspace.organization_id == org.id)) if org else None
    return {"id": row.id, "name": row.name, "email": row.email, "company": row.company, "domain": row.domain, "status": row.status, "note": row.note,
            "marketing_consent": bool(row.marketing_consent), "unsubscribed": bool(row.unsubscribed_at),
            "source": row.utm_source, "medium": row.utm_medium, "campaign": row.utm_campaign, "term": row.utm_term, "content": row.utm_content,
            "created_at": row.created_at.isoformat() if row.created_at else None, "started_at": row.started_at.isoformat() if row.started_at else None,
            "ends_at": row.ends_at.isoformat() if row.ends_at else None, "days_left": days_left(row.ends_at, now) if row.status == "active" else 0,
            "keywords": len(ws.keywords or []) if ws else 0, "ai_drafts": (org.trial_ai_drafts or 0) if org else 0,
            "ask_messages": (org.trial_ask_messages or 0) if org else 0, "plan": org.plan if org and org.billing_status == "active" else "",
            "billing_status": org.billing_status if org else ""}


def list_signups(db: Session, *, status: str = "", q: str = "", limit_: int = 50, offset: int = 0, now: datetime | None = None) -> dict:
    now = _now(now)
    stmt = select(TrialSignup)
    if status:
        stmt = stmt.where(TrialSignup.status == status)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(TrialSignup.email).like(like), func.lower(TrialSignup.company).like(like), func.lower(TrialSignup.name).like(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(TrialSignup.created_at.desc()).limit(max(1, min(limit_, 200))).offset(max(0, offset))).all()
    return {"total": total, "rows": [_row_dict(db, r, now) for r in rows]}


CSV_COLUMNS = ["name", "email", "company", "domain", "status", "marketing_consent", "unsubscribed", "created_at", "started_at", "ends_at", "days_left",
               "source", "medium", "campaign", "term", "content", "keywords", "ai_drafts", "ask_messages", "plan", "note"]


def _safe_cell(v) -> str:
    """Stops a spreadsheet treating a typed value as a formula (a name that starts with = or + could otherwise run code when the file is opened)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def export_csv(db: Session, now: datetime | None = None) -> str:
    now = _now(now)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    for row in db.scalars(select(TrialSignup).order_by(TrialSignup.created_at.desc())).all():
        d = _row_dict(db, row, now)
        w.writerow([_safe_cell("yes" if d[c] is True else "no" if d[c] is False else d[c]) for c in CSV_COLUMNS])
    return out.getvalue()


# ------------------------------------------------------------------ the nurture sequence
def _step(key, label, kind, when, days, subject, body):
    return {"key": key, "label": label, "kind": kind, "when": when, "days": days, "enabled": True, "subject": subject, "body": body}


_CTA = '<p><a href="{{app_url}}/login" style="background:#1D4ED8;color:#ffffff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">Open your dashboard</a></p>'
_SIGN = "<p>Emmanuel<br>Founder, BrandsLens</p>"
DEFAULT_STEPS = [
    _step("welcome", "Welcome (sent at once)", "transactional", "after_start", 0, "Your BrandsLens trial has started",
          "<p>Hi {{first_name}},</p><p>Your free BrandsLens trial is live, and it runs until <b>{{ends_on}}</b>. You've got a real Professional workspace for {{company}}, with some limits so you can feel the product without the full load.</p>"
          "<p>Start here: check the five keywords we set up for you, then watch what comes in. Anything serious is scored HIGH, MEDIUM or WATCH.</p>" + _CTA +
          "<p>Reply to this email any time and I'll help.</p>" + _SIGN),
    _step("day1_setup", "Day 1: set up", "marketing", "after_start", 1, "Your first alert in 3 minutes",
          "<p>Hi {{first_name}},</p><p>The fastest way to see BrandsLens work is to make sure it is watching the right things. Open <b>Settings</b>, review your keywords, and add the names people use for {{company}}.</p>"
          "<p>Within minutes you'll start seeing mentions, each scored so the serious ones rise to the top.</p>" + _CTA + _SIGN),
    _step("day3_clock", "Day 3: the response clock", "marketing", "after_start", 3, "Why the first 4 hours matter",
          "<p>Hi {{first_name}},</p><p>When a mention is scored HIGH, BrandsLens opens a case and starts a response clock (4 hours for HIGH). That clock is the point: a problem is decided in the first hours, not the first week.</p>"
          "<p>Open the <b>Media Room</b> to see how a case is tracked from detection to a sent statement.</p>" + _CTA + _SIGN),
    _step("day7_respond", "Day 7: respond", "marketing", "after_start", 7, "From alert to an approved statement",
          "<p>Hi {{first_name}},</p><p>Seen something worth answering? Use <b>AI Draft Statement</b> on a case to get a first draft, edit it, and send it only after it is approved. You stay in control and still move fast.</p>" + _CTA + _SIGN),
    _step("day14_prove", "Day 14: prove it", "marketing", "after_start", 14, "Show leadership what you found",
          "<p>Hi {{first_name}},</p><p>Two weeks in, you can turn what BrandsLens found into something leadership understands: Share of Voice, Estimated Media Value and a report you can download.</p>" + _CTA + _SIGN),
    _step("before_end_7", "7 days before the end", "marketing", "before_end", 7, "7 days left on your trial",
          "<p>Hi {{first_name}},</p><p>Your trial ends on <b>{{ends_on}}</b>. If BrandsLens has been useful, you can choose a plan any time and keep your workspace exactly as it is. Paid plans also remove the trial limits.</p>"
          "{{offer_line}}<p><a href=\"{{upgrade_url}}\" style=\"background:#1D4ED8;color:#ffffff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:700;display:inline-block\">Choose a plan</a></p>" + _SIGN),
    _step("before_end_3", "3 days before the end", "transactional", "before_end", 3, "Your trial ends in 3 days",
          "<p>Hi {{first_name}},</p><p>Your free trial ends on <b>{{ends_on}}</b>, in about {{days_left}} days. After that your workspace is paused until you choose a plan.</p>"
          "{{offer_line}}<p><a href=\"{{upgrade_url}}\">Choose a plan</a></p>" + _SIGN),
    _step("before_end_1", "1 day before the end", "transactional", "before_end", 1, "Your trial ends tomorrow",
          "<p>Hi {{first_name}},</p><p>A quick reminder: your free trial ends on <b>{{ends_on}}</b>. To keep monitoring {{company}} without a break, choose a plan before then.</p>"
          "<p><a href=\"{{upgrade_url}}\">Choose a plan</a></p>" + _SIGN),
    _step("ended", "When it ends", "transactional", "after_end", 0, "Your BrandsLens trial has ended",
          "<p>Hi {{first_name}},</p><p>Your free trial has ended and monitoring is paused. Your workspace is kept, so if you choose a plan you can pick up where you left off.</p>"
          "{{offer_line}}<p><a href=\"{{upgrade_url}}\">Choose a plan</a></p><p>If something stopped you from continuing, just reply and tell me.</p>" + _SIGN),
    _step("after_end_3", "3 days after the end", "marketing", "after_end", 3, "Pick up where you left off",
          "<p>Hi {{first_name}},</p><p>Your {{company}} workspace is still here. Choose a plan and monitoring resumes straight away, with everything you set up still in place.</p>"
          "{{offer_line}}<p><a href=\"{{upgrade_url}}\">Choose a plan</a></p>" + _SIGN),
    _step("after_end_10", "10 days after the end", "marketing", "after_end", 10, "Still thinking it over?",
          "<p>Hi {{first_name}},</p><p>If you have questions about plans, or want a walk-through for your team, reply to this email and I'll arrange it.</p>" + _SIGN),
]
STEP_KEYS = {s["key"] for s in DEFAULT_STEPS}


def steps(db: Session) -> list[dict]:
    saved = {s.get("key"): s for s in (get_setting(db, K["steps"], []) or []) if isinstance(s, dict)}
    out = []
    for d in DEFAULT_STEPS:
        s, o = dict(d), saved.get(d["key"], {})
        for field in ("subject", "body"):
            if isinstance(o.get(field), str) and o[field].strip():
                s[field] = o[field]
        if isinstance(o.get("enabled"), bool):
            s["enabled"] = o["enabled"]
        if isinstance(o.get("days"), int) and not isinstance(o.get("days"), bool) and 0 <= o["days"] <= 365:
            s["days"] = o["days"]
        out.append(s)
    return out


def save_steps(db: Session, actor: str, items: list) -> list[dict]:
    if not isinstance(items, list):
        raise TrialError("Send a list of steps.")
    keep = []
    for it in items:
        if not isinstance(it, dict) or it.get("key") not in STEP_KEYS:
            raise TrialError("Unknown email step.")
        subject, body = str(it.get("subject", "")).strip(), str(it.get("body", "")).strip()
        if not subject or len(subject) > 200:
            raise TrialError("Each email needs a subject of up to 200 characters.")
        if not body or len(body) > 20000:
            raise TrialError("Each email needs a message of up to 20,000 characters.")
        days = it.get("days")
        if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= 365:
            raise TrialError("Days must be a whole number from 0 to 365.")
        if not isinstance(it.get("enabled"), bool):
            raise TrialError("Each step must be switched on or off.")
        keep.append({"key": it["key"], "subject": subject, "body": body, "days": days, "enabled": it["enabled"]})
    set_setting(db, K["steps"], keep, updated_by=actor)
    db.commit()
    return steps(db)


def _scheduled(step: dict, row: TrialSignup) -> datetime | None:
    if not row.started_at or not row.ends_at:
        return None
    start, end, d = aware(row.started_at), aware(row.ends_at), timedelta(days=step["days"])
    return {"after_start": start + d, "before_end": end - d, "after_end": end + d}.get(step["when"])


def due_steps(db: Session, row: TrialSignup, now: datetime) -> list[tuple[datetime, dict]]:
    now = _now(now)
    done = {e.step_key for e in db.scalars(select(TrialEmail).where(TrialEmail.signup_id == row.id)).all()}
    start, end, out = aware(row.started_at), aware(row.ends_at), []
    for s in steps(db):
        if not s["enabled"] or s["key"] in done:
            continue
        t = _scheduled(s, row)
        if t is None or t > now:
            continue
        # a short or extended trial: skip steps that would land at the very start, or after the end for a "during the trial" step
        if s["key"] != "welcome" and s["when"] == "before_end" and t < start + timedelta(hours=1):
            continue
        if s["when"] == "after_start" and s["key"] != "welcome" and t >= end - timedelta(hours=12):
            continue
        if s["when"] == "before_end" and t >= end:
            continue
        out.append((t, s))
    return sorted(out, key=lambda x: x[0])


def _vars(db: Session, row: TrialSignup) -> dict:
    ends = aware(row.ends_at)
    code = get_settings(db)["offer_code"]
    offer = (f"<p>Use code <b>{html.escape(code)}</b> at checkout for your launch discount.</p>" if code else "")
    return {"first_name": html.escape((row.name or "").split(" ")[0] or "there"), "company": html.escape(row.company or "your company"),
            "ends_on": ends.strftime("%d %B %Y") if ends else "", "days_left": str(days_left(row.ends_at)), "app_url": APP_URL,
            "upgrade_url": f"{APP_URL}/billing", "offer_line": offer,
            "unsubscribe_url": f"{PUBLIC_API_URL}/api/public/trial/unsubscribe?token={row.unsub_token}"}


def render(text: str, variables: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: variables.get(m.group(1), ""), text)


def build_email(db: Session, row: TrialSignup, step: dict) -> tuple[str, str]:
    from . import mailer
    v = _vars(db, row)
    foot = ('<p style="color:#6b7280;font-size:12px">You are getting this because you started a BrandsLens free trial. '
            f'<a href="{v["unsubscribe_url"]}">Unsubscribe</a> from tips and offers.</p>' if step["kind"] == "marketing" else
            '<p style="color:#6b7280;font-size:12px">This is a message about your BrandsLens trial. Just reply if you need anything.</p>')
    return render(step["subject"], {k: re.sub(r"<[^>]+>", "", x) for k, x in v.items()}), mailer._wrap(render(step["body"], v) + foot)


def send_step(db: Session, row: TrialSignup, step: dict, to: str | None = None) -> bool:
    from . import mailer
    subject, body = build_email(db, row, step)
    return mailer.send_email(to or row.email, subject, body, from_addr=TRIAL_FROM, reply_to=TRIAL_REPLY_TO)


def run_nurture_for(db: Session, row: TrialSignup, now: datetime | None = None) -> int:
    """Sends the latest step that is due. Earlier ones that were overtaken (say, the server was switched off for a week) are recorded as
    skipped and not sent, so nobody ever gets a burst of emails at once."""
    now = _now(now)
    if row.status in ("blocked", "pending", "requested", "approved") or not row.started_at:
        return 0
    due = due_steps(db, row, now)
    if not due:
        return 0
    *older, (_, latest) = due
    for _, s in older:
        db.add(TrialEmail(signup_id=row.id, step_key=s["key"], skipped=True))
    sent = 0
    may_market = bool(row.marketing_consent) and not row.unsubscribed_at
    if latest["kind"] == "marketing" and not may_market:
        db.add(TrialEmail(signup_id=row.id, step_key=latest["key"], skipped=True))
    elif send_step(db, row, latest):
        db.add(TrialEmail(signup_id=row.id, step_key=latest["key"]))
        sent = 1
    db.commit()
    return sent


def run_nurture(db: Session, now: datetime | None = None, batch: int = 300) -> int:
    now = _now(now)
    total = 0
    for row in db.scalars(select(TrialSignup).where(TrialSignup.status.in_(("active", "expired", "converted")),
                                                    TrialSignup.started_at.isnot(None)).limit(batch)).all():
        if row.status == "converted":
            continue                                    # a paying customer is no longer nurtured as a trial lead
        try:
            total += run_nurture_for(db, row, now)
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            db.rollback()
            log.exception("Nurture failed for lead %s", row.id)
    return total


def run_lifecycle(db: Session, now: datetime | None = None) -> dict:
    now = _now(now)
    return {"expired": expire_due_trials(db, now), "emails": run_nurture(db, now)}


def unsubscribe(db: Session, token: str) -> bool:
    row = db.scalar(select(TrialSignup).where(TrialSignup.unsub_token == (token or ""))) if token else None
    if not row:
        return False
    row.marketing_consent, row.unsubscribed_at = False, now_utc()
    db.commit()
    try:                                                # an opt-out must stop the Mailchimp campaigns too, not just our own sequence
        from . import mailer
        mailer.unsubscribe_from_mailchimp(row.email)
    except Exception:  # noqa: BLE001
        log.exception("Could not unsubscribe the trial lead in Mailchimp")
    return True


def send_test(db: Session, step_key: str, to: str) -> bool:
    step = next((s for s in steps(db) if s["key"] == step_key), None)
    if not step:
        raise TrialError("Unknown email step.")
    if not _EMAIL.match(normalise_email(to)):
        raise TrialError("Enter a valid email address.")
    now = now_utc()
    sample = TrialSignup(email=normalise_email(to), domain=domain_of(to), name="Ada Okafor", company="Example Ltd", status="active",
                         started_at=now, ends_at=now + timedelta(days=30), unsub_token="test-preview")
    return send_step(db, sample, step, to=normalise_email(to))
