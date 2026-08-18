"""app/deps.py — FastAPI dependencies for auth. Every protected route depends
on current_member (or require_role(...)) rather than trusting anything in the
request body about who the caller is or what organization they belong to.
"""
from __future__ import annotations
from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session

from .db import get_db
from .services.auth import member_from_token
from .models import OrgMember, Workspace, Organization, now_utc, aware


def current_member(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> OrgMember:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing bearer token")
    member = member_from_token(db, token)
    if not member:
        raise HTTPException(401, "Invalid or expired session")
    return member


def require_role(*roles: str):
    def _check(member: OrgMember = Depends(current_member)) -> OrgMember:
        if member.role not in roles:
            raise HTTPException(403, f"Requires one of: {', '.join(roles)}")
        return member
    return _check


def active_member(member: OrgMember = Depends(current_member), db: Session = Depends(get_db)) -> OrgMember:
    """Same as current_member, but additionally enforces the paywall: no
    free trial — an organization starts 'unpaid' and stays gated until a
    real payment succeeds. The one exception is billing_status='exempt',
    reserved for the single admin account created via
    scripts/create_admin.py — never reachable through public signup.
    Deliberately NOT used by /api/auth/* or /api/billing/* routes — someone
    who's locked out must still be able to log in and pay to unlock
    themselves; only actual product data is gated by this."""
    org = db.get(Organization, member.organization_id)
    if org.billing_status == "exempt":
        return member
    if org.billing_status != "active":
        raise HTTPException(402, "Payment required to activate your BrandsLens account.")
    return member


def owned_workspace(ws_id: str, member: OrgMember = Depends(active_member), db: Session = Depends(get_db)) -> Workspace:
    """The single most important dependency in this file: it's what stops one
    organization from ever reading or mutating another's workspace, no matter
    what ID a client passes in the URL."""
    ws = db.get(Workspace, ws_id)
    if not ws or ws.organization_id != member.organization_id:
        raise HTTPException(404, "Workspace not found")
    return ws
