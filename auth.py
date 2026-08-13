"""app/services/auth.py — real accounts, replacing the flat MVP_API_KEY.

Every rule here is enforced server-side, never trusting anything the client
claims about its own role or organization — that's what actually makes one
client's data private from another's.
"""
from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from passlib.hash import argon2
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Organization, OrgMember, OrgInvite, SessionToken, Workspace, now_utc, aware

INVITE_TTL_HOURS = 72
SESSION_TTL_DAYS = 30
PLAN_WORKSPACE_LIMIT = {"professional": 1, "corp_growth": 3, "enterprise": 999}
PLAN_KEYWORD_LIMIT = {"professional": 5, "corp_growth": 999, "enterprise": 999}


class AuthError(Exception):
    """Raised for anything a client should see as a clean 4xx, not a 500."""


def hash_password(raw: str) -> str:
    return argon2.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return bool(hashed) and argon2.verify(raw, hashed)


def signup(db: Session, company: str, sector: str, plan: str,
          name: str, email: str, password: str) -> tuple[OrgMember, str]:
    """Creates the organization, its first member (role=owner, active
    immediately), a starter workspace, and returns a live session token."""
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    if db.scalar(select(OrgMember).where(OrgMember.email == email.lower())):
        raise AuthError("An account with this email already exists. Try logging in instead.")

    plan = plan if plan in PLAN_WORKSPACE_LIMIT else "professional"
    org = Organization(name=company, sector=sector, plan=plan,
                       workspace_limit=PLAN_WORKSPACE_LIMIT[plan],
                       keyword_limit=PLAN_KEYWORD_LIMIT[plan], billing_status="trialing")
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
                           f"impersonating {company}", f"{company} refund"])
    db.add(ws)
    db.commit()

    token = issue_session(db, owner)
    return owner, token


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
    existing = db.scalars(select(Workspace).where(Workspace.organization_id == org.id)).all()
    if len(existing) >= org.workspace_limit:
        raise AuthError(f"Your plan allows up to {org.workspace_limit} workspace(s). Upgrade to add more.")
    ws = Workspace(organization_id=org.id, name=name, sector=sector, owner_email=actor.email,
                  brand_tokens=[t.strip().lower() for t in name.split() if len(t.strip()) > 2] or [name.lower()])
    db.add(ws)
    db.commit()
    return ws
