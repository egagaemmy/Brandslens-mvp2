"""demo_seed.py — a fictional workspace for recording the tutorial videos.

Builds three subscriber accounts (Standard, Growth, Professional) for a
made up brand, Acme Foods, each with realistic data: mentions across
several platforms, Media Room cases in different states, escalation
contacts, competitors, a team, and referral earnings.

Two rules shape everything here:
  * It is flagged is_demo, and every system job checks that flag. A demo
    workspace is never scanned (a made up brand would only pull in real
    coverage of real companies), never emailed, and never counted as a
    subscriber.
  * Everything goes through the real services (open_case, transition,
    approve, send_escalation), so the Media Room audit trails are genuine
    hash chains, not hand written rows.

Dates are relative to the moment of seeding. Re-seed with reset before a
recording block so 'three hours remaining' really is three hours remaining.
"""
from __future__ import annotations
import hashlib
import logging
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (Base, Organization, OrgMember, Workspace, Incident, MediaRoomCase, EscalationContact,
                      ThreatCategory, Competitor, CompetitorMention, Referrer, ReferralCommission, now_utc)
from . import auth, media_room

log = logging.getLogger("demo_seed")
DEMO_DOMAIN = "acme-demo.example"      # a reserved domain: it can never receive real mail
PLANS = ["standard", "growth", "professional"]
COMPETITORS = ["Brightbite Foods", "Harvest Table Ltd", "Zesto Kitchen"]
KEYWORDS = ["Acme Foods", "Acme Foods recall", "Acme Foods scam", "Acme Jollof Mix", "Tola Adeyemi"]

# (title, platform, severity, sentiment, tags, hours ago, reach, status)
HIGH = [
    ("Fake Acme Foods giveaway asks fans to pay a delivery fee", "X", "HIGH", "Negative", ["FRAUD"], 1, 18400, "open"),
    ("acmefoods-promo.example copies the Acme Foods website to collect card details", "Domain Watch", "HIGH", "Negative", ["DOMAIN RISK"], 96, 3200, "resolved"),
    ("Cloned Acme Foods page offers staff discount orders on Facebook", "Facebook", "HIGH", "Negative", ["FRAUD"], 170, 9800, "resolved"),
    ("Draft of Acme Foods plant announcement circulates before the official statement", "Nairaland", "HIGH", "Negative", ["DISCLOSURE RISK"], 2, 21500, "open"),
]
MEDIUM = [
    ("Viral thread claims Acme Jollof Mix contains undeclared allergens", "Reddit", "MEDIUM", "Negative", [], 6, 14200, "open"),
    ("Customers report delayed Acme Foods deliveries across three cities", "X", "MEDIUM", "Negative", [], 1, 7600, "open"),
    ("Review video disputes Acme Foods nutrition claims", "YouTube", "MEDIUM", "Negative", [], 60, 38000, "in review"),
    ("Headline overstates the size of the Acme Foods price rise", "News", "MEDIUM", "Negative", [], 80, 92000, "resolved"),
    ("Retailer group chat warns about expired Acme stock", "Facebook", "MEDIUM", "Negative", [], 120, 4100, "resolved"),
    ("Blogger questions where Acme Foods sources its ingredients", "News", "MEDIUM", "Negative", [], 200, 12300, "resolved"),
    ("Forum users compare Acme packaging with a suspected counterfeit batch", "Nairaland", "MEDIUM", "Negative", ["FRAUD"], 240, 5800, "resolved"),
    ("Petition against the Acme Foods plant location gains signatures", "Facebook", "MEDIUM", "Negative", [], 300, 16900, "dismissed"),
]
# recent week, the campaign spike about ten to twelve days ago, a quieter stretch, then older items found by a historical search
WATCH = [
    ("Acme Foods named among the region's fastest growing food brands", "News", "Neutral", 30, 54000),
    ("Loving the new Acme Jollof Mix, dinner done in ten minutes", "X", "Positive", 9, 2300),
    ("Acme Foods community cook off returns this month", "Facebook", "Positive", 40, 6500),
    ("Five quick meals with Acme Jollof Mix", "YouTube", "Positive", 70, 21000),
    ("Which instant mixes are actually worth buying", "Reddit", "Neutral", 50, 4800),
    ("The Acme sachet was torn when I opened it, disappointed", "X", "Negative", 14, 900),
    ("Where to buy Acme Foods near me", "Facebook", "Neutral", 20, 1200),
    ("Acme Foods trending after the new television advert", "X", "Neutral", 5, 11500),
    ("Acme Foods unveils its summer campaign", "News", "Positive", 240, 61000),
    ("Industry watch: the Acme Foods campaign draws attention", "News", "Neutral", 246, 28000),
    ("Acme Foods announces its campaign ambassador", "News", "Positive", 252, 47000),
    ("Early reactions to the Acme Foods campaign", "News", "Neutral", 258, 19000),
    ("The campaign hashtag is trending for Acme Foods", "X", "Positive", 264, 33000),
    ("Acme Foods campaign: what people are saying", "X", "Neutral", 270, 9500),
    ("Behind the scenes of the Acme Foods advert shoot", "Facebook", "Positive", 276, 12400),
    ("Acme Foods campaign jingle review", "YouTube", "Neutral", 288, 17000),
    ("Acme Foods opens a new distribution centre", "News", "Positive", 380, 38000),
    ("Student cooks competition sponsored by Acme Foods", "Facebook", "Positive", 450, 5200),
    ("Acme Foods joins the packaging recycling pledge", "News", "Positive", 520, 29000),
    ("Acme Foods appoints a new head of marketing", "News", "Neutral", 600, 22000),
    ("A first look at the Acme Foods recipe app", "YouTube", "Positive", 660, 8800),
    ("Acme Foods stall draws crowds at the food fair", "Facebook", "Positive", 700, 6100),
    # older, found by a historical search
    ("Acme Foods marks ten years in business", "News", "Positive", 1800, 44000),
    ("Acme Foods supplier dispute reported", "News", "Negative", 3100, 26000),
    ("Acme Jollof Mix wins a consumer choice award", "News", "Positive", 4300, 52000),
    ("Acme Foods profile: from market stall to national brand", "News", "Positive", 5600, 31000),
    ("Customers react to the Acme Foods packaging redesign", "Reddit", "Neutral", 7200, 7400),
    ("Acme Foods expands into a second country", "News", "Neutral", 9000, 48000),
]
HISTORICAL_FROM_HOURS = 1000
CONTACTS = [("Funmi Okoro", "Head of Communications", "comms", "misinfo"), ("Segun Balogun", "Legal Counsel", "legal", "regulator"),
            ("Kemi Adamu", "Head of Security", "security", "fraud")]
TEAM = [("Tola Adeyemi", "owner", "Managing Director"), ("Chidi Nwosu", "lead", "Head of Communications"), ("Amara Eze", "member", "Brand Analyst")]
EXTRA_CATEGORIES = [("retailer", "Retailer Dispute"), ("safety", "Product Safety Notice")]


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def demo_orgs(db: Session) -> list[Organization]:
    return list(db.scalars(select(Organization).where(Organization.is_demo == True)).all())  # noqa: E712


# ------------------------------------------------------------------ removal
def _cascade_delete(db: Session, table, ids, seen=None) -> None:
    """Deletes rows and everything that points at them, following the real
    foreign keys, so removing a demo account can never leave orphans and
    never needs a hand maintained list of tables."""
    seen = seen if seen is not None else set()
    ids = [i for i in ids if i is not None]
    if not ids: return
    for child in Base.metadata.tables.values():
        for fk in child.foreign_keys:
            if fk.column.table is not table: continue
            key = (child.name, fk.parent.name, tuple(sorted(map(str, ids))))
            if key in seen: continue
            seen.add(key)
            pk = list(child.primary_key.columns)
            if len(pk) == 1:
                child_ids = [r[0] for r in db.execute(select(pk[0]).where(fk.parent.in_(ids))).all()]
                _cascade_delete(db, child, child_ids, seen)
            else:
                db.execute(delete(child).where(fk.parent.in_(ids)))
    pk = list(table.primary_key.columns)[0]
    db.execute(delete(table).where(pk.in_(ids)))


def remove_demo(db: Session) -> dict:
    orgs = demo_orgs(db)
    emails = [m.email for o in orgs for m in db.scalars(select(OrgMember).where(OrgMember.organization_id == o.id)).all()]
    ref_ids = [r.id for r in db.scalars(select(Referrer).where(Referrer.email.in_(emails))).all()] if emails else []
    _cascade_delete(db, Referrer.__table__, ref_ids)
    _cascade_delete(db, Organization.__table__, [o.id for o in orgs])
    db.commit()
    return {"removed_organizations": len(orgs)}


# ------------------------------------------------------------------ creation
def _make_org(db: Session, plan: str, owner_password: str) -> tuple[Organization, Workspace, OrgMember]:
    now = now_utc()
    org = Organization(name=f"Acme Foods ({plan.title()} demo)", sector="FMCG", plan=plan,
                       workspace_limit=auth.PLAN_WORKSPACE_LIMIT[plan], keyword_limit=auth.PLAN_KEYWORD_LIMIT[plan],
                       billing_status="active", billing_cycle="annual", plan_activated_at=now,
                       paid_until=now + timedelta(days=3650), is_demo=True)
    db.add(org); db.flush()
    owner_email = f"{plan}.demo@{DEMO_DOMAIN}"
    ws = Workspace(organization_id=org.id, name="Acme Foods", sector="FMCG", owner_email=owner_email,
                   brand_tokens=["acme foods", "acmefoods"], keywords=list(KEYWORDS), rss_feeds=list(auth.DEFAULT_RSS_FEEDS),
                   brand_domains=["acmefoods.example"], telegram_channels=["@acmefoodsdemo"])
    db.add(ws); db.flush()
    auth._seed_default_threat_categories(db, ws)
    for key, label in EXTRA_CATEGORIES: db.add(ThreatCategory(workspace_id=ws.id, key=key, label=label))
    owner = None
    for name, role, title in TEAM:
        email = owner_email if role == "owner" else f"{name.split()[0].lower()}.{plan}@{DEMO_DOMAIN}"
        m = OrgMember(organization_id=org.id, email=email, name=name, role=role, status="active", job_title=title,
                      password_hash=auth.hash_password(owner_password if role == "owner" else secrets.token_urlsafe(16)),
                      activated_at=now, invited_at=now, country="Nigeria", city="Lagos")
        db.add(m)
        if role == "owner": owner = m
    db.flush()
    return org, ws, owner


def _make_incidents(db: Session, ws: Workspace) -> list[Incident]:
    now, rows, n = now_utc(), [], 0
    entries = [(t, p, sv, se, tg, h, r, st) for (t, p, sv, se, tg, h, r, st) in HIGH + MEDIUM]
    entries += [(t, p, "WATCH", se, [], h, r, "open" if i % 5 else "resolved") for i, (t, p, se, h, r) in enumerate(WATCH)]
    entries.sort(key=lambda e: -e[5])           # oldest first, so reference numbers run in time order
    for (title, platform, sev, sent, tags, hours, reach, status) in entries:
        n += 1
        posted = now - timedelta(hours=hours)
        rationale = {"HIGH": "Impersonates or exposes the brand, with a direct risk to customers or the business.",
                     "MEDIUM": "Negative and spreading. Needs a response or a decision this week."}.get(sev, "Routine mention. Worth knowing, no action needed yet.")
        inc = Incident(workspace_id=ws.id, ref=f"ACM-{n:03d}", title=title, url=f"https://example.com/acme-demo/{n:03d}",
                       author=f"Demo author {n}", platform=platform, severity=sev, sentiment=sent, tags=list(tags), rationale=rationale,
                       matched_keywords=["Acme Foods"], status=status, reach=reach, content_hash=_hash(ws.id, str(n), title),
                       source="demo", found_historically=hours >= HISTORICAL_FROM_HOURS, posted_at=posted, logged_at=posted + timedelta(minutes=12))
        db.add(inc); rows.append(inc)
    db.flush()
    return rows


_PATH = ["under_review", "classified", "alerted", "statement_drafted", "sent", "closed"]


def _advance(db: Session, case: MediaRoomCase, stop_at: str | None, actor: str) -> None:
    for state in _PATH:
        if case.state == stop_at: return
        if case.state == "statement_drafted" and case.requires_approval and not case.approved_by:
            media_room.approve(db, case, actor)
        media_room.transition(db, case, state, actor)


def _make_cases(db: Session, ws: Workspace, incidents: list[Incident], contacts: list[EscalationContact]) -> None:
    now = now_utc()
    comms = contacts[0]
    for inc in incidents:
        if inc.severity == "WATCH": continue
        case = media_room.open_case(db, inc)
        age_h = (now - inc.posted_at).total_seconds() / 3600
        case.sla_started_at = inc.posted_at
        if inc.ref and inc.title.startswith("Draft of Acme Foods plant"):       # the case that needs written approval
            case.playbook_key, case.requires_approval = "regulator", True
            _advance(db, case, "statement_drafted", "Funmi Okoro")
        elif age_h <= 3:
            _advance(db, case, "classified" if inc.severity == "HIGH" else "detected", "Chidi Nwosu")
        elif age_h <= 48:
            _advance(db, case, "under_review", "Chidi Nwosu")
        else:
            _advance(db, case, None, "Chidi Nwosu")                          # worked through to closed
            media_room.send_escalation(db, case, case.playbook_key or "general", comms.name, comms.email,
                                       f"Action taken: {inc.title}", "The matter has been handled and the response was sent.", "Chidi Nwosu")
    db.flush()


def _make_competitors(db: Session, ws: Workspace, plan: str) -> None:
    now = now_utc()
    sentiments = ["Positive", "Neutral", "Negative", "Neutral", "Positive"]
    for ci, name in enumerate(COMPETITORS[: auth.PLAN_COMPETITOR_LIMIT[plan]]):
        comp = Competitor(workspace_id=ws.id, name=name); db.add(comp); db.flush()
        for k in range([18, 12, 7][ci]):
            h = 6 + k * (700 // max(1, [18, 12, 7][ci]))
            db.add(CompetitorMention(competitor_id=comp.id, workspace_id=ws.id, text=f"{name} mention {k + 1}",
                                     url=f"https://example.com/{name.split()[0].lower()}/{k + 1}", platform=["News", "X", "Facebook", "YouTube"][k % 4],
                                     sentiment=sentiments[(k + ci) % 5], content_hash=_hash(ws.id, name, str(k)), posted_at=now - timedelta(hours=h)))


def _make_referral(db: Session, owner: OrgMember) -> None:
    from . import referrals
    ref, _ = referrals.create_referrer(db, owner.name, owner.email, "individual", "", source="member", member_id=owner.id)
    now = now_utc()
    for name, amount, cur, age_d, hold_left_d, paid in [("Demo referred company A", 2500, "USD", 5, 25, False), ("Demo referred company B", 3500, "USD", 40, -10, False),
                                                         ("Demo referred company C", 1500, "USD", 75, -45, True), ("Demo referred company D", 3750000, "NGN", 3, 27, False)]:
        status = "paid" if paid else ("pending" if hold_left_d > 0 else "approved")
        db.add(ReferralCommission(referrer_id=ref.id, organization_id=str(uuid.uuid4()), organization_name=name, payment_ref=f"demo-{name[-1]}",
                                  amount_paid=amount, currency=cur, rate_percent=3.0, commission=round(amount * 0.03, 2), status=status,
                                  approvable_at=now + timedelta(days=hold_left_d), paid_at=(now - timedelta(days=30)) if paid else None,
                                  created_at=now - timedelta(days=age_d)))
    ref.payout_details = "Demo Bank, account 0000000000, Tola Adeyemi"


def seed_demo(db: Session, reset: bool = False) -> dict:
    existing = demo_orgs(db)
    if existing and not reset:
        return {"status": "exists", "message": "Demo accounts already exist. Add reset=true to rebuild them with fresh dates and new passwords."}
    if existing:
        remove_demo(db)
    accounts = []
    for plan in PLANS:
        password = secrets.token_urlsafe(9)
        org, ws, owner = _make_org(db, plan, password)
        contacts = []
        for name, title, local, cat in CONTACTS:
            c = EscalationContact(workspace_id=ws.id, name=name, title=title, email=f"{local}@{DEMO_DOMAIN}", category=cat)
            db.add(c); contacts.append(c)
        db.flush()
        incidents = _make_incidents(db, ws)
        _make_cases(db, ws, incidents, contacts)
        _make_competitors(db, ws, plan)
        _make_referral(db, owner)
        db.commit()
        accounts.append({"plan": plan, "email": owner.email, "password": password, "workspace_id": ws.id, "workspace": ws.name})
    log.info("Demo data seeded for %d accounts", len(accounts))
    return {"status": "seeded", "accounts": accounts,
            "note": "Passwords are shown once. Re-run with reset=true for new ones. Demo accounts are never scanned, emailed or counted as subscribers."}
