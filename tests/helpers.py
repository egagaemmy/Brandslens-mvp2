from datetime import timedelta

from sqlalchemy import select

from app.models import Incident, Organization, Workspace, now_utc
from app.services import auth, promos


def make_customer(db, plan="growth", email="owner@example.com", password="correct-horse-1", company="Customer Co", active=True):
    """A real, paid customer account (not a demo): returns (member, token, workspace)."""
    member, token = auth.signup(db, company, "FMCG", plan, "Owner Person", email, password)
    org = db.get(Organization, member.organization_id)
    if active:
        org.billing_status = "active"
        org.paid_until = now_utc() + timedelta(days=365)
        db.commit()
    ws = db.scalar(select(Workspace).where(Workspace.organization_id == org.id))
    return member, token, ws


def make_admin(db, email="admin@brandslens.test", password="admin-pass-123"):
    member, token = auth.create_exempt_admin(db, "BrandsLens", "Admin", email, password)
    return member, token


def add_incident(db, ws, title="A mention", severity="WATCH", rationale="", **kw):
    inc = Incident(workspace_id=ws.id, ref=kw.pop("ref", f"REF-{abs(hash(title)) % 100000}"), title=title, severity=severity,
                   rationale=rationale, content_hash=kw.pop("content_hash", f"h-{abs(hash(title)) % 10**9}"), **kw)
    db.add(inc)
    db.commit()
    return inc


def seed_valid_codes(db, percent=15):
    """The four launch codes, valid for a very long window starting in the past, so tests are not date-sensitive."""
    return promos.seed_launch_codes(db, percent=percent, starts_on="2020-01-01", days=4000)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}
