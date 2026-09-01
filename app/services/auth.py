"""app/services/auth.py — real accounts, replacing the flat MVP_API_KEY.

Every rule here is enforced server-side, never trusting anything the client
claims about its own role or organization — that's what actually makes one
client's data private from another's.
"""
from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from passlib.hash import argon2
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import Organization, OrgMember, OrgInvite, SessionToken, PasswordResetToken, Workspace, now_utc, aware

INVITE_TTL_HOURS = 72
RESET_TTL_HOURS = 2
SESSION_TTL_DAYS = 30
PLAN_WORKSPACE_LIMIT = {"standard": 1, "growth": 5, "professional": 10, "enterprise": 999}

# Sensible starter RSS feeds for a new workspace — a mix of major Nigerian
# outlets and international sources with strong Africa coverage, so a new
# customer sees real signal on day one instead of an empty list they have
# to populate by hand. These are well-known, conventionally-structured feed
# URLs based on each outlet's standard pattern; RSS endpoints do sometimes
# change or get discontinued, so this list is worth spot-checking against
# the live sites periodically rather than treated as permanently correct.
DEFAULT_RSS_FEEDS = [
    # Nigerian outlets (15)
    "https://punchng.com/feed/",
    "https://www.vanguardngr.com/feed/",
    "https://guardian.ng/feed/",
    "https://www.premiumtimesng.com/feed/",
    "https://dailytrust.com/feed/",
    "https://www.thisdaylive.com/index.php/feed/",
    "https://businessday.ng/feed/",
    "https://nairametrics.com/feed/",
    "https://techcabal.com/feed/",
    "https://leadership.ng/feed/",
    "https://www.thecable.ng/feed",
    "https://dailypost.ng/feed/",
    "https://tribuneonlineng.com/feed/",
    "http://saharareporters.com/feed",
    "https://thenationonlineng.net/feed/",
    # International, with strong Africa coverage (10)
    "http://feeds.bbci.co.uk/news/world/africa/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf",
    "https://www.africanews.com/feed/rss",
    "https://www.france24.com/en/africa/rss",
    "https://www.theguardian.com/world/africa/rss",
    "https://www.cnbcafrica.com/feed/",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "http://rss.cnn.com/rss/edition_africa.rss",
    "https://www.devex.com/rss/news",
]

# Sensible starter threat categories — matching media_room.PLAYBOOKS exactly,
# so a freshly created workspace's escalation contacts line up with how
# incidents actually get classified out of the box. Genuinely editable
# after this: see the ThreatCategory model's own docstring for why.
DEFAULT_THREAT_CATEGORIES = [
    ("fraud", "Fraud / Impersonation Alert"),
    ("domain", "Look-Alike Domain Takedown"),
    ("disclosure", "Premature Disclosure Warning"),
    ("misinfo", "Misinformation Correction"),
    ("regulator", "Regulator Notification"),
    ("general", "General Escalation"),
]


def _seed_default_threat_categories(db: Session, ws: "Workspace") -> None:
    from ..models import ThreatCategory
    for key, label in DEFAULT_THREAT_CATEGORIES:
        db.add(ThreatCategory(workspace_id=ws.id, key=key, label=label))
PLAN_KEYWORD_LIMIT = {"standard": 25, "growth": 999, "professional": 999, "enterprise": 999}
PLAN_MEMBER_LIMIT = {"standard": 3, "growth": 10, "professional": 25, "enterprise": 999}
# Historical search reach, in days. 0 means the feature isn't available at
# all on that tier — Standard doesn't get it.
PLAN_HISTORICAL_DAYS = {"standard": 0, "growth": 365 * 5, "professional": 365 * 10, "enterprise": 365 * 100}
# The in-app "Ask BrandsLens" copilot (distinct from the public marketing
# bot) — not available on Standard at all. Growth gets straightforward data
# Q&A; Professional and Enterprise get a deeper analyst mode that's asked
# to reason about patterns and give real recommendations, not just recite
# numbers back.
PLAN_APP_CHAT_MODE = {"standard": None, "growth": "basic", "professional": "advanced", "enterprise": "advanced"}
PLAN_COMPETITOR_LIMIT = {"standard": 2, "growth": 5, "professional": 8, "enterprise": 999}
# Report format access, cumulative by tier — Excel is available to everyone,
# PDF from Growth up, PPTX from Professional up.
PLAN_REPORT_FORMATS = {"standard": {"excel"}, "growth": {"excel", "pdf"},
                       "professional": {"excel", "pdf", "pptx"}, "enterprise": {"excel", "pdf", "pptx"}}
SELF_SERVE_PLANS = ("standard", "growth", "professional")  # Enterprise is quote-only, never self-serve


class AuthError(Exception):
    """Raised for anything a client should see as a clean 4xx, not a 500."""


def hash_password(raw: str) -> str:
    return argon2.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return bool(hashed) and argon2.verify(raw, hashed)


def effective_plan(org: Organization) -> str:
    """What the org's limits and UI should actually reflect right now. Only
    ever differs from `plan` for the exempt admin account using the View As
    simulator — everyone else always sees their own real plan."""
    if org.billing_status == "exempt" and org.view_as_plan:
        return org.view_as_plan
    return org.plan


def signup(db: Session, company: str, sector: str, plan: str,
          name: str, email: str, password: str) -> tuple[OrgMember, str]:
    """Creates the organization, its first member (role=owner), and a starter
    workspace. No free trial: billing_status starts 'unpaid', which the
    active_member dependency (see deps.py) hard-gates immediately — real
    payment is required before any product access, by design."""
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    if plan not in SELF_SERVE_PLANS:
        raise AuthError("That plan requires a custom quote — use the Enterprise contact form instead.")
    if db.scalar(select(OrgMember).where(OrgMember.email == email.lower())):
        raise AuthError("An account with this email already exists. Try logging in instead.")

    org = Organization(name=company, sector=sector, plan=plan,
                       workspace_limit=PLAN_WORKSPACE_LIMIT[plan],
                       keyword_limit=PLAN_KEYWORD_LIMIT[plan], billing_status="unpaid")
    db.add(org)
    db.flush()

    owner = OrgMember(organization_id=org.id, email=email.lower(), name=name,
                      password_hash=hash_password(password), role="owner", status="active",
                      activated_at=now_utc())
    db.add(owner)
    db.flush()

    starter_tokens = [t.strip().lower() for t in company.split() if len(t.strip()) > 2] or [company.lower()]
    ws = Workspace(organization_id=org.id, name=company, sector=sector, owner_email=email.lower(),
                  brand_tokens=starter_tokens,
                  keywords=[f"{company} scam", f"{company} fraud", f"fake {company}",
                           f"impersonating {company}", f"{company} refund"],
                  rss_feeds=list(DEFAULT_RSS_FEEDS))
    db.add(ws)
    db.flush()
    _seed_default_threat_categories(db, ws)
    db.commit()

    token = issue_session(db, owner)
    return owner, token


def create_exempt_admin(db: Session, company: str, name: str, email: str, password: str) -> tuple[OrgMember, str]:
    """Not reachable from any public route — run only via scripts/create_admin.py.
    billing_status='exempt' means deps.active_member never gates this
    organization, regardless of plan or view_as_plan."""
    if db.scalar(select(OrgMember).where(OrgMember.email == email.lower())):
        raise AuthError("An account with this email already exists.")
    org = Organization(name=company, sector="Internal", plan="enterprise",
                       workspace_limit=PLAN_WORKSPACE_LIMIT["enterprise"],
                       keyword_limit=PLAN_KEYWORD_LIMIT["enterprise"], billing_status="exempt")
    db.add(org)
    db.flush()
    owner = OrgMember(organization_id=org.id, email=email.lower(), name=name,
                      password_hash=hash_password(password), role="owner", status="active",
                      activated_at=now_utc())
    db.add(owner)
    db.flush()
    ws = Workspace(organization_id=org.id, name=company, sector="Internal", owner_email=email.lower(),
                  brand_tokens=[t.strip().lower() for t in company.split() if len(t.strip()) > 2] or [company.lower()])
    db.add(ws)
    db.commit()
    token = issue_session(db, owner)
    return owner, token


def set_view_as(db: Session, org: Organization, plan: str | None) -> None:
    """Admin-only tier simulator. `plan=None` resets to the real (unlimited)
    view. Only ever callable when billing_status=='exempt' — enforced by the
    route, not here, but double-checked here too since this changes real
    enforcement limits, not just a display label."""
    if org.billing_status != "exempt":
        raise AuthError("View As is only available on the exempt admin account.")
    if plan is not None and plan not in PLAN_WORKSPACE_LIMIT:
        raise AuthError("Unknown plan.")
    org.view_as_plan = plan
    db.commit()


def invite_member(db: Session, inviter: OrgMember, email: str, name: str, role: str) -> str:
    """Enforces the role hierarchy server-side: a Team Lead can invite Team
    Members only; a Member cannot invite anyone; only the Owner can create
    another Team Lead. Returns the invite link's token."""
    if role not in ("lead", "member"):
        raise AuthError("Role must be 'lead' or 'member'.")
    if inviter.role == "member":
        raise AuthError("Team Members cannot invite anyone.")
    if inviter.role == "lead" and role != "member":
        raise AuthError("Team Leads can only invite Team Members. Ask the account Owner to add another Lead.")
    if db.scalar(select(OrgMember).where(OrgMember.email == email.lower())):
        raise AuthError("That email is already on a BrandsLens account.")

    org = db.get(Organization, inviter.organization_id)
    limit = PLAN_MEMBER_LIMIT[effective_plan(org)]
    existing_count = db.scalar(select(func.count(OrgMember.id)).where(OrgMember.organization_id == org.id))
    if existing_count >= limit:
        raise AuthError(f"Your plan allows up to {limit} team members. Upgrade to add more.")

    member = OrgMember(organization_id=inviter.organization_id, email=email.lower(), name=name,
                       role=role, status="invited", invited_by=inviter.id)
    db.add(member)
    db.flush()
    invite = OrgInvite(member_id=member.id, token=secrets.token_urlsafe(24),
                       expires_at=now_utc() + timedelta(hours=INVITE_TTL_HOURS))
    db.add(invite)
    db.commit()
    return invite.token
    # In production: email a link like https://brandseye.app/accept-invite?token=...
    # rather than returning the raw token to the caller to display.


def accept_invite(db: Session, token: str, password: str) -> tuple[OrgMember, str]:
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    invite = db.scalar(select(OrgInvite).where(OrgInvite.token == token))
    if not invite or invite.used_at or aware(invite.expires_at) < now_utc():
        raise AuthError("This invite link is invalid or has expired.")
    member = db.get(OrgMember, invite.member_id)
    member.password_hash = hash_password(password)
    member.status = "active"
    member.activated_at = now_utc()
    invite.used_at = now_utc()
    db.commit()
    session_token = issue_session(db, member)
    return member, session_token


def authenticate(db: Session, email: str, password: str) -> tuple[OrgMember, str]:
    member = db.scalar(select(OrgMember).where(OrgMember.email == email.lower(), OrgMember.status == "active"))
    if not member or not verify_password(password, member.password_hash):
        raise AuthError("Incorrect email or password.")
    member.last_login_at = now_utc()
    db.commit()
    token = issue_session(db, member)
    return member, token


def issue_session(db: Session, member: OrgMember) -> str:
    token = secrets.token_urlsafe(32)
    db.add(SessionToken(member_id=member.id, token=token, expires_at=now_utc() + timedelta(days=SESSION_TTL_DAYS)))
    db.commit()
    return token


def revoke_session(db: Session, token: str) -> None:
    row = db.scalar(select(SessionToken).where(SessionToken.token == token))
    if row:
        row.revoked_at = now_utc()
        db.commit()


def member_from_token(db: Session, token: str) -> OrgMember | None:
    row = db.scalar(select(SessionToken).where(SessionToken.token == token))
    if not row or row.revoked_at or aware(row.expires_at) < now_utc():
        return None
    member = db.get(OrgMember, row.member_id)
    if not member or member.status != "active":
        return None
    return member


def remove_member(db: Session, actor: OrgMember, target_id: str) -> None:
    target = db.get(OrgMember, target_id)
    if not target or target.organization_id != actor.organization_id:
        raise AuthError("Member not found in your organization.")
    if target.role == "owner":
        raise AuthError("The account Owner cannot be removed.")
    if actor.role == "member":
        raise AuthError("Team Members cannot remove anyone.")
    if actor.role == "lead" and target.role != "member":
        raise AuthError("Team Leads can only remove Team Members.")
    db.delete(target)
    db.commit()


def add_workspace(db: Session, actor: OrgMember, name: str, sector: str) -> Workspace:
    if actor.role == "member":
        raise AuthError("Only the account Owner or a Team Lead can add a workspace.")
    org = db.get(Organization, actor.organization_id)
    limit = PLAN_WORKSPACE_LIMIT[effective_plan(org)]
    existing = db.scalars(select(Workspace).where(Workspace.organization_id == org.id)).all()
    if len(existing) >= limit:
        raise AuthError(f"Your plan allows up to {limit} workspace(s). Upgrade to add more.")
    ws = Workspace(organization_id=org.id, name=name, sector=sector, owner_email=actor.email,
                  brand_tokens=[t.strip().lower() for t in name.split() if len(t.strip()) > 2] or [name.lower()],
                  rss_feeds=list(DEFAULT_RSS_FEEDS))
    db.add(ws)
    db.flush()
    _seed_default_threat_categories(db, ws)
    db.commit()
    return ws


def request_password_reset(db: Session, email: str) -> str | None:
    """Always call this the same way regardless of outcome — the API layer
    must return an identical response whether or not the email exists, so
    this endpoint can never be used to check who has an account. Returns
    the token only for the caller to email; never for the API to echo back."""
    member = db.scalar(select(OrgMember).where(OrgMember.email == email.lower(), OrgMember.status == "active"))
    if not member:
        return None
    token = secrets.token_urlsafe(32)
    db.add(PasswordResetToken(member_id=member.id, token=token,
                             expires_at=now_utc() + timedelta(hours=RESET_TTL_HOURS)))
    db.commit()
    return token


def reset_password(db: Session, token: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    row = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token == token))
    if not row or row.used_at or aware(row.expires_at) < now_utc():
        raise AuthError("This reset link is invalid or has expired. Request a new one.")
    member = db.get(OrgMember, row.member_id)
    member.password_hash = hash_password(new_password)
    row.used_at = now_utc()
    db.commit()


MAX_AVATAR_BASE64_CHARS = 900_000  # ~650KB decoded — plenty for a profile photo, small enough to keep in a text column sanely

def update_profile(db: Session, member: OrgMember, **fields) -> OrgMember:
    """Anyone can edit their own profile — no role restriction, since this is
    personal data about the member themselves, not an organization setting."""
    if "avatar_base64" in fields and fields["avatar_base64"]:
        avatar = fields["avatar_base64"]
        if not avatar.startswith("data:image/"):
            raise AuthError("Profile picture must be a valid image.")
        if len(avatar) > MAX_AVATAR_BASE64_CHARS:
            raise AuthError("That image is too large — please use something under ~500KB.")
    for field in ("name", "phone", "address", "city", "country", "job_title", "avatar_base64"):
        if field in fields and fields[field] is not None:
            setattr(member, field, fields[field])
    db.commit()
    return member
