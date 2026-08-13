"""BrandsLens MVP — database models.

Deliberately lean: one table set, SQLite by default so there is nothing to
install to run this today. Swap DATABASE_URL to Postgres later (see db.py) —
SQLAlchemy means the rest of the code doesn't change.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (String, Text, Integer, BigInteger, Boolean, DateTime,
                        ForeignKey, Index, UniqueConstraint, JSON)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite doesn't preserve timezone info the way Postgres does — a
    DateTime(timezone=True) column comes back naive after a round trip
    through SQLite. Everything this app stores is UTC by convention, so
    treating a naive value as UTC here is correct, not a guess, and this
    keeps every comparison working identically on both databases."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Organization(Base):
    """One paying (or trialing) subscriber. Every workspace, every member,
    every incident traces back to exactly one of these — this is the boundary
    that makes accounts genuinely private from each other."""
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    sector: Mapped[str] = mapped_column(String(120), default="")
    plan: Mapped[str] = mapped_column(String(20), default="professional")   # professional / corp_growth / enterprise
    workspace_limit: Mapped[int] = mapped_column(Integer, default=1)        # None-equivalent: a very large int for "unlimited"
    keyword_limit: Mapped[int] = mapped_column(Integer, default=5)
    billing_provider: Mapped[str] = mapped_column(String(20), default="")   # 'stripe' | 'paystack' | '' (never paid)
    billing_customer_id: Mapped[str] = mapped_column(String(120), default="")
    billing_subscription_id: Mapped[str] = mapped_column(String(120), default="")
    billing_status: Mapped[str] = mapped_column(String(20), default="trialing")
    # 'trialing' | 'active' | 'past_due' | 'expired' | 'cancelled' — see services/billing.py
    trial_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    plan_activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    plan_cancelled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    read_only_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    members: Mapped[list["OrgMember"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    workspaces: Mapped[list["Workspace"]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class OrgMember(Base):
    """A real person who can log in. UNIQUE(email) is deliberate: one email
    belongs to exactly one organization, which is what actually makes an
    account private — nobody can end up straddling two subscribers' data."""
    __tablename__ = "org_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300), default="")   # empty until invite is accepted
    role: Mapped[str] = mapped_column(String(10), default="member")       # owner / lead / member
    status: Mapped[str] = mapped_column(String(12), default="invited")    # invited / active / suspended
    invited_by: Mapped[str] = mapped_column(String(36), default="")
    invited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- Profile / KYC-style fields, editable after signup ----
    avatar_base64: Mapped[str] = mapped_column(Text, default="")   # data URL, e.g. "data:image/png;base64,..."
    phone: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    country: Mapped[str] = mapped_column(String(120), default="")
    job_title: Mapped[str] = mapped_column(String(160), default="")

    organization: Mapped["Organization"] = relationship(back_populates="members")


class OrgInvite(Base):
    """Single-use, expiring invite token. The invitee sets their OWN password
    when accepting — nobody, including the person who invited them, ever
    sees or chooses another member's password."""
    __tablename__ = "org_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    member_id: Mapped[str] = mapped_column(ForeignKey("org_members.id"), index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class SessionToken(Base):
    """Server-issued, revocable. Replaces the flat MVP_API_KEY — every request
    now resolves to exactly one member of exactly one organization."""
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    member_id: Mapped[str] = mapped_column(ForeignKey("org_members.id"), index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class BillingEvent(Base):
    """Append-only log of every webhook received, so a billing dispute or a
    'why did my plan change' ticket can always be traced to the exact event."""
    __tablename__ = "billing_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(36), default="")
    provider: Mapped[str] = mapped_column(String(20))
    event_type: Mapped[str] = mapped_column(String(60))
    raw: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Workspace(Base):
    """One brand being monitored, owned by exactly one organization."""
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    sector: Mapped[str] = mapped_column(String(120), default="")
    owner_email: Mapped[str] = mapped_column(String(200), default="")
    brand_tokens: Mapped[list] = mapped_column(JSON, default=list)     # e.g. ["brandseye", "brands eye"]
    keywords: Mapped[list] = mapped_column(JSON, default=list)        # fraud/keyword phrases to watch for
    rss_feeds: Mapped[list] = mapped_column(JSON, default=list)
    brand_domains: Mapped[list] = mapped_column(JSON, default=list)   # official domains, for typosquat comparison
    telegram_channels: Mapped[list] = mapped_column(JSON, default=list)
    reddit_subreddits: Mapped[list] = mapped_column(JSON, default=list)
    youtube_query: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    organization: Mapped["Organization"] = relationship(back_populates="workspaces")
    incidents: Mapped[list["Incident"]] = relationship(back_populates="workspace")


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        Index("ix_inc_ws_posted", "workspace_id", "posted_at"),
        UniqueConstraint("workspace_id", "content_hash", name="uq_inc_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    ref: Mapped[str] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    platform: Mapped[str] = mapped_column(String(60), default="News")
    lang: Mapped[str] = mapped_column(String(8), default="en")
    severity: Mapped[str] = mapped_column(String(10), default="WATCH")   # HIGH / MEDIUM / WATCH
    sentiment: Mapped[str] = mapped_column(String(12), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)              # FRAUD, DOMAIN RISK, DISCLOSURE RISK, STRATEGIC
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="open")
    reach: Mapped[int] = mapped_column(BigInteger, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(40), default="crawl")
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    workspace: Mapped["Workspace"] = relationship(back_populates="incidents")


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    candidates: Mapped[int] = mapped_column(Integer, default=0)
    new_incidents: Mapped[int] = mapped_column(Integer, default=0)
    high_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


# ==================================================================
# MEDIA ROOM — the escalation protocol, in full (from architecture blueprint §3)
# ==================================================================
class MediaRoomCase(Base):
    __tablename__ = "media_room_cases"
    __table_args__ = (Index("ix_mrc_ws_state", "workspace_id", "state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    incident_id: Mapped[str] = mapped_column(String(36), default="")
    severity: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(24), default="detected")
    # detected -> under_review -> classified -> alerted -> statement_drafted
    #   -> pending_approval (regulator track only) -> sent -> closed
    sla_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    sla_target_hours: Mapped[float] = mapped_column(default=4.0)
    sla_breached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    playbook_key: Mapped[str] = mapped_column(String(30), default="")   # fraud / domain / disclosure / misinfo
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class MediaRoomStatement(Base):
    __tablename__ = "media_room_statements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("media_room_cases.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    drafted_by: Mapped[str] = mapped_column(String(200), default="ai")
    recipients: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class MediaRoomAudit(Base):
    """Append-only, hash-chained. See services/media_room.py — every row's hash
    covers the previous row's, so tampering with history is detectable at read
    time, not just policy. No UPDATE/DELETE should ever be granted on this
    table in production (see architecture blueprint §3)."""
    __tablename__ = "media_room_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("media_room_cases.id"), index=True)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class TipLineMessage(Base):
    """Raw inbound forwards from the Telegram tip-line bot, before they're
    turned into (or rejected from becoming) an Incident. Keeping the raw
    message lets a human review anything the classifier was unsure about."""
    __tablename__ = "tipline_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    from_telegram_id: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    forwarded_from: Mapped[str] = mapped_column(String(200), default="")  # original sender if it's a forward
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    incident_id: Mapped[str] = mapped_column(String(36), default="")      # set once triaged
