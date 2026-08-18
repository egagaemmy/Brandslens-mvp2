# BrandsLens — Backend Architecture Blueprint (v4)

Companion to the interactive frontend build. This document specs the real backend
for the modules added in this round: the Media Room (SLA clock + crisis workflow),
the Regional Dialect NLP Engine, Emotion Detection, and AI Visibility Score tracking,
on top of the multi-tenant platform already outlined in the earlier backend package
(FastAPI + PostgreSQL + collectors + Claude classification pipeline).

Stack, as specified: **Next.js/React + Tailwind** (frontend), **FastAPI** for NLP and
scoring inference, **Node.js** for lighter I/O-bound services if you choose to split
them, **PostgreSQL with Row-Level Security** for tenant isolation, **Redis** for
queues/caching, **OpenSearch** for high-speed mention querying, **WebSockets/SSE**
for live updates.

---

## 1. System Schema (additions to the existing platform)

The base tables (`clients`, `incidents`, `keywords`, `escalations`, `users`,
`scan_runs`, `audit_events`) are unchanged from the earlier backend package. This
section adds what the new modules need.

```sql
-- ============ ROW-LEVEL SECURITY (multi-tenant isolation) ============
-- Every tenant-scoped table gets a tenant_id and an RLS policy. Example for incidents;
-- repeat the pattern for keywords, escalations, audit_events, emotion_scores, ai_visibility_scans.

ALTER TABLE incidents ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_incidents ON incidents
  USING (client_id = current_setting('app.current_client_id')::uuid);

-- The API sets `app.current_client_id` per request via:
--   SET LOCAL app.current_client_id = '<client_uuid>';
-- inside the same transaction as the query, derived from the authenticated user's
-- workspace membership — never trusted from client input.


-- ============ PLAN / TENANCY ENFORCEMENT ============
CREATE TYPE plan_tier AS ENUM ('professional', 'corp_growth', 'enterprise');

CREATE TABLE organizations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  plan plan_tier NOT NULL DEFAULT 'professional',
  billing_cycle TEXT NOT NULL DEFAULT 'annual',       -- 'annual' | 'monthly'
  workspace_limit INT NOT NULL DEFAULT 1,              -- 1 / 3 / NULL(unlimited) by plan
  keyword_limit INT,                                   -- 5 for professional, NULL = unlimited
  mention_limit INT NOT NULL DEFAULT 15000,            -- monthly cap, enforced by a nightly job
  created_at TIMESTAMPTZ DEFAULT now()
);

-- workspace/client rows (existing `clients` table) gain:
ALTER TABLE clients ADD COLUMN organization_id UUID REFERENCES organizations(id);

-- Enforcement is at the API layer (see §2), backstopped by a DB trigger so a bug
-- in application logic can never silently exceed the contracted tier:
CREATE OR REPLACE FUNCTION enforce_workspace_limit() RETURNS TRIGGER AS $$
DECLARE
  current_count INT;
  org_limit INT;
BEGIN
  SELECT workspace_limit INTO org_limit FROM organizations WHERE id = NEW.organization_id;
  SELECT COUNT(*) INTO current_count FROM clients WHERE organization_id = NEW.organization_id;
  IF org_limit IS NOT NULL AND current_count >= org_limit THEN
    RAISE EXCEPTION 'workspace_limit_exceeded';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_workspace_limit BEFORE INSERT ON clients
  FOR EACH ROW EXECUTE FUNCTION enforce_workspace_limit();


-- ============ EMOTION DETECTION ============
CREATE TABLE emotion_scores (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  joy NUMERIC(4,3) DEFAULT 0,
  anger NUMERIC(4,3) DEFAULT 0,
  fear NUMERIC(4,3) DEFAULT 0,
  sadness NUMERIC(4,3) DEFAULT 0,
  surprise NUMERIC(4,3) DEFAULT 0,
  dominant_emotion TEXT,                 -- denormalized for fast filtering/sort
  model_version TEXT NOT NULL,           -- e.g. 'emotion-xlmr-v2'
  scored_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ix_emotion_incident ON emotion_scores(incident_id);


-- ============ REGIONAL DIALECT NLP ENGINE ============
CREATE TABLE dialect_lexicons (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lang_code TEXT NOT NULL,                -- 'pcm', 'yo', 'ha', 'ig', 'sw', 'fr-cr'
  term TEXT NOT NULL,
  polarity_base NUMERIC(3,2),             -- -1..1 baseline sentiment for this term
  is_sarcasm_marker BOOLEAN DEFAULT FALSE,-- e.g. "una well done" (Pidgin sarcasm cue)
  context_flip BOOLEAN DEFAULT FALSE,     -- term whose polarity often flips under sarcasm
  notes TEXT,
  UNIQUE(lang_code, term)
);

CREATE TABLE dialect_classifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  lang_code TEXT NOT NULL,
  raw_sentiment TEXT NOT NULL,            -- what a generic model would have said
  adjusted_sentiment TEXT NOT NULL,       -- after dialect + sarcasm adjustment
  sarcasm_detected BOOLEAN DEFAULT FALSE,
  sarcasm_confidence NUMERIC(4,3),
  adjustment_reason TEXT,                 -- human-readable, shown in the UI as "engine flag"
  model_version TEXT NOT NULL,
  classified_at TIMESTAMPTZ DEFAULT now()
);


-- ============ MEDIA ROOM (crisis workflow) ============
CREATE TYPE media_room_state AS ENUM (
  'detected','under_review','classified','alerted',
  'statement_drafted','pending_approval','sent','closed'
);

CREATE TABLE media_room_cases (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES clients(id),
  incident_id UUID REFERENCES incidents(id),
  severity TEXT NOT NULL,                 -- HIGH / MEDIUM / WATCH
  state media_room_state NOT NULL DEFAULT 'detected',
  sla_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sla_target_hours NUMERIC(4,1) NOT NULL DEFAULT 4.0,
  sla_breached_at TIMESTAMPTZ,             -- set by a scheduled job when the clock lapses unresolved
  playbook_key TEXT,                       -- 'fraud' | 'domain' | 'disclosure' | 'misinfo' | 'regulator'
  requires_regulator_approval BOOLEAN DEFAULT FALSE,
  approved_by UUID REFERENCES users(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ix_mrc_client_state ON media_room_cases(client_id, state);

CREATE TABLE media_room_statements (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  case_id UUID NOT NULL REFERENCES media_room_cases(id) ON DELETE CASCADE,
  version INT NOT NULL DEFAULT 1,
  drafted_by TEXT NOT NULL,                -- 'ai' | user email
  body TEXT NOT NULL,
  recipients TEXT,
  ai_assisted BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Immutable audit trail: append-only, hash-chained so a row can never be edited
-- without breaking the chain (detectable at read time, not just "trust us").
CREATE TABLE media_room_audit (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  case_id UUID NOT NULL REFERENCES media_room_cases(id) ON DELETE CASCADE,
  actor TEXT NOT NULL,                     -- user email or 'system'
  action TEXT NOT NULL,
  detail JSONB,
  prev_hash TEXT NOT NULL,                 -- hash of the previous row in this case's chain
  row_hash TEXT NOT NULL,                  -- sha256(prev_hash || actor || action || detail || at)
  at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- No UPDATE or DELETE grants on this table for any application role — INSERT only.
-- REVOKE UPDATE, DELETE ON media_room_audit FROM app_user;


-- ============ AI VISIBILITY SCORE ============
CREATE TABLE ai_visibility_scans (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES clients(id),
  ran_at TIMESTAMPTZ DEFAULT now(),
  overall_score INT,                       -- 0-100
  citation_frequency NUMERIC(5,2)          -- % of prompts where brand was cited
);

CREATE TABLE ai_visibility_results (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id UUID NOT NULL REFERENCES ai_visibility_scans(id) ON DELETE CASCADE,
  model TEXT NOT NULL,                     -- 'claude' | 'chatgpt' | 'gemini' | 'perplexity'
  prompt TEXT NOT NULL,
  response_excerpt TEXT,
  cited BOOLEAN NOT NULL,
  sentiment TEXT,                          -- Favorable / Neutral / Unfavorable
  model_score INT                          -- 0-100 for this model on this prompt set
);
```

---

## 2. API Routes (additions)

All routes below sit under the existing FastAPI app, behind the same bearer-token
auth. Every handler opens its DB transaction with `SET LOCAL app.current_client_id`
resolved from the authenticated user's workspace membership, never from a URL or
body parameter — this is what makes the RLS policies actually enforceable.

```
# --- Media Room ---
GET    /api/media-room/cases?client_code=&state=&severity=
POST   /api/media-room/cases                     # opens a case from an incident
PATCH  /api/media-room/cases/{id}/state           # transition state, validated against
                                                   # the allowed state machine edges
POST   /api/media-room/cases/{id}/statements      # save a draft (version += 1)
POST   /api/media-room/cases/{id}/ai-draft        # calls the LLM drafting service (§4)
POST   /api/media-room/cases/{id}/approve         # regulator-track only; requires
                                                   # role=reviewer|admin AND requires_regulator_approval
GET    /api/media-room/cases/{id}/audit           # returns the hash-chained trail,
                                                   # server verifies the chain before returning

# --- Emotion Detection ---
GET    /api/incidents/{id}/emotion
POST   /api/nlp/emotion/batch                     # internal: pipeline worker call

# --- Regional Dialect Engine ---
GET    /api/dialects/{lang_code}/lexicon
POST   /api/dialects/{lang_code}/lexicon          # add/update a term (admin only)
POST   /api/nlp/dialect/classify                  # internal: pipeline worker call

# --- AI Visibility ---
POST   /api/ai-visibility/scan?client_code=       # triggers a scan (rate-limited per plan)
GET    /api/ai-visibility/latest?client_code=
GET    /api/ai-visibility/history?client_code=&days=

# --- Tenancy / plan enforcement ---
GET    /api/organizations/{id}/limits              # workspace_limit, keyword_limit, mention_limit, usage
POST   /api/organizations/{id}/workspaces          # returns 409 workspace_limit_exceeded if at cap
```

---

## 3. Module: Media Room — SLA Clock & Crisis Workflow

State machine, enforced server-side so the UI can never push an illegal transition:

```
detected → under_review → classified → alerted → statement_drafted
    → pending_approval (regulator track only) → sent → closed
```

```python
"""app/services/media_room.py — SLA clock + state machine + hash-chained audit."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import MediaRoomCase, MediaRoomAudit, Incident

ALLOWED_TRANSITIONS = {
    "detected": {"under_review"},
    "under_review": {"classified"},
    "classified": {"alerted"},
    "alerted": {"statement_drafted"},
    "statement_drafted": {"pending_approval", "sent"},   # skip approval if not regulator-track
    "pending_approval": {"sent", "under_review"},         # rejection sends it back
    "sent": {"closed"},
}

SLA_TARGET_HOURS = {"HIGH": 4.0, "MEDIUM": 24.0, "WATCH": 72.0}


def open_case(db: Session, incident: Incident) -> MediaRoomCase:
    """Every HIGH incident auto-opens a Media Room case on ingestion (see pipeline.py)."""
    case = MediaRoomCase(
        client_id=incident.client_id,
        incident_id=incident.id,
        severity=incident.severity,
        sla_target_hours=SLA_TARGET_HOURS.get(incident.severity, 24.0),
        requires_regulator_approval=incident.severity == "HIGH" and "FRAUD" in incident.tags,
    )
    db.add(case)
    db.flush()
    _audit(db, case, actor="system", action="case_opened",
           detail={"incident_ref": incident.ref, "severity": incident.severity})
    return case


def transition(db: Session, case: MediaRoomCase, new_state: str, actor: str,
              detail: dict | None = None) -> MediaRoomCase:
    if new_state not in ALLOWED_TRANSITIONS.get(case.state, set()):
        raise ValueError(f"Illegal transition {case.state} -> {new_state}")
    if new_state == "sent" and case.requires_regulator_approval and not case.approved_by:
        raise ValueError("Regulator-track case requires approval before sending")
    old_state = case.state
    case.state = new_state
    case.updated_at = datetime.now(timezone.utc)
    _audit(db, case, actor=actor, action="state_change",
           detail={"from": old_state, "to": new_state, **(detail or {})})
    return case


def sla_remaining_hours(case: MediaRoomCase) -> float:
    elapsed = (datetime.now(timezone.utc) - case.sla_started_at).total_seconds() / 3600
    return case.sla_target_hours - elapsed


def sweep_sla_breaches(db: Session) -> list[MediaRoomCase]:
    """Run every few minutes by the scheduler. Marks lapsed cases and returns them
    so the caller can fire escalation notifications (Slack/email/webhook)."""
    open_cases = db.scalars(
        select(MediaRoomCase).where(MediaRoomCase.state.notin_(["sent", "closed"]))
    ).all()
    breached = []
    for case in open_cases:
        if sla_remaining_hours(case) <= 0 and not case.sla_breached_at:
            case.sla_breached_at = datetime.now(timezone.utc)
            _audit(db, case, actor="system", action="sla_breached",
                   detail={"target_hours": case.sla_target_hours})
            breached.append(case)
    db.commit()
    return breached


def _audit(db: Session, case: MediaRoomCase, actor: str, action: str, detail: dict) -> MediaRoomAudit:
    """Append-only, hash-chained. Each row's hash covers the previous row's hash,
    so any tampering with history breaks verification (see verify_chain)."""
    last = db.scalars(
        select(MediaRoomAudit).where(MediaRoomAudit.case_id == case.id)
        .order_by(MediaRoomAudit.at.desc()).limit(1)
    ).first()
    prev_hash = last.row_hash if last else "genesis"
    at = datetime.now(timezone.utc)
    payload = json.dumps({"actor": actor, "action": action, "detail": detail, "at": at.isoformat()},
                         sort_keys=True)
    row_hash = hashlib.sha256((prev_hash + payload).encode()).hexdigest()
    entry = MediaRoomAudit(case_id=case.id, actor=actor, action=action, detail=detail,
                           prev_hash=prev_hash, row_hash=row_hash, at=at)
    db.add(entry)
    return entry


def verify_chain(db: Session, case_id: str) -> bool:
    """Recomputes every hash in a case's audit trail. Called before returning the
    trail to a client (GET /media-room/cases/{id}/audit) — a tampered row fails here."""
    rows = db.scalars(
        select(MediaRoomAudit).where(MediaRoomAudit.case_id == case_id).order_by(MediaRoomAudit.at)
    ).all()
    prev = "genesis"
    for row in rows:
        payload = json.dumps({"actor": row.actor, "action": row.action, "detail": row.detail,
                              "at": row.at.isoformat()}, sort_keys=True)
        expected = hashlib.sha256((prev + payload).encode()).hexdigest()
        if expected != row.row_hash or row.prev_hash != prev:
            return False
        prev = row.row_hash
    return True
```

**AI-assisted statement drafting** (`/media-room/cases/{id}/ai-draft`) calls Claude
with a system prompt that constrains it to draft, not decide — legal review of any
generated statement is enforced as a workflow gate (`pending_approval`), not left
to the model:

```python
"""app/services/statement_composer.py"""
from anthropic import Anthropic
from ..config import ANTHROPIC_API_KEY

SYSTEM = """You draft crisis and regulatory communications for a PR agency's Media
Room. You are drafting ONLY — never state that a matter is resolved, admit fault,
or make legal claims. Always include a placeholder for legal/compliance review.
Keep statements factual, calm, and free of speculation about unverified details."""

def draft_statement(client_name: str, incident_summary: str, template_type: str) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5", max_tokens=600, system=SYSTEM,
        messages=[{"role": "user", "content":
            f"Client: {client_name}\nTemplate type: {template_type}\n"
            f"Incident summary: {incident_summary}\n\nDraft the statement."}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")
```

---

## 4. Module: Regional Dialect NLP Engine

Two-stage pipeline: a fast lexicon-based pass catches known dialect sarcasm markers
cheaply, then anything ambiguous escalates to an LLM pass with dialect-specific
few-shot examples. This mirrors why the earlier classifier design routes through
a keyword prefilter before spending model tokens.

```python
"""app/services/dialect_engine.py"""
from __future__ import annotations
import re
from sqlalchemy import select
from sqlalchemy.orm import Session
from anthropic import Anthropic
from ..models import DialectLexicon, DialectClassification
from ..config import ANTHROPIC_API_KEY

# Seed sarcasm markers — extend via the /dialects/{lang}/lexicon admin endpoint,
# ideally curated with native-speaker reviewers per market.
SEED_SARCASM_MARKERS = {
    "pcm": ["una well done", "we thank God", "na wa o", "chai see gobe"],
    "yo": ["e ku ise o", "adupe o"],           # ironic "well done" / "thank you" patterns
    "ha": ["mun gode sosai"],                   # ironic "thank you very much"
    "ig": ["daalu nke ukwu"],                   # ironic "thanks a lot"
}

DIALECT_FEW_SHOT = {
    "pcm": [
        ("Una well done for this refinery wahala, we thank God oo", "Negative", True),
        ("This IPO na correct opportunity, make we gather money", "Positive", False),
    ],
    "yo": [
        ("E ku ise o, e da wa laseku gan ni", "Negative", True),
    ],
}

SYSTEM = """You classify sentiment for social media text in African languages and
dialects, with special attention to sarcasm and irony that generic sentiment models
misread. Sarcastic praise reads as positive on the surface but is negative in intent.
Respond ONLY with JSON: {"sentiment":"Positive|Negative|Neutral","sarcasm":true|false,
"confidence":0.0-1.0,"reason":"<short phrase>"}"""


def lexicon_prefilter(db: Session, lang_code: str, text: str) -> dict | None:
    """Cheap first pass. Returns a classification dict if a known sarcasm marker
    matches with high lexical confidence, else None (falls through to the LLM pass)."""
    markers = db.scalars(
        select(DialectLexicon.term).where(
            DialectLexicon.lang_code == lang_code, DialectLexicon.is_sarcasm_marker == True)  # noqa: E712
    ).all() or SEED_SARCASM_MARKERS.get(lang_code, [])
    lower = text.lower()
    for marker in markers:
        if marker.lower() in lower:
            return {"sentiment": "Negative", "sarcasm": True, "confidence": 0.72,
                    "reason": f"matched known sarcasm marker: '{marker}'"}
    return None


def classify(db: Session, incident_id: str, lang_code: str, text: str,
            raw_sentiment: str) -> DialectClassification:
    result = lexicon_prefilter(db, lang_code, text)
    if result is None:
        result = _llm_classify(lang_code, text)

    entry = DialectClassification(
        incident_id=incident_id, lang_code=lang_code,
        raw_sentiment=raw_sentiment, adjusted_sentiment=result["sentiment"],
        sarcasm_detected=result["sarcasm"], sarcasm_confidence=result["confidence"],
        adjustment_reason=result["reason"], model_version="dialect-engine-v1",
    )
    db.add(entry)
    return entry


def _llm_classify(lang_code: str, text: str) -> dict:
    if not ANTHROPIC_API_KEY:
        return {"sentiment": "Neutral", "sarcasm": False, "confidence": 0.0,
                "reason": "fallback — dialect engine unavailable"}
    examples = DIALECT_FEW_SHOT.get(lang_code, [])
    example_block = "\n".join(
        f'Text: "{t}"\nAnswer: {{"sentiment":"{s}","sarcasm":{str(sc).lower()}}}'
        for t, s, sc in examples
    )
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5", max_tokens=200, system=SYSTEM,
            messages=[{"role": "user", "content":
                f"Language: {lang_code}\n\nExamples:\n{example_block}\n\n"
                f'Now classify:\nText: "{text[:600]}"'}],
        )
        import json
        text_out = "".join(b.text for b in resp.content if b.type == "text").strip()
        text_out = text_out.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text_out)
    except Exception:  # noqa: BLE001
        return {"sentiment": "Neutral", "sarcasm": False, "confidence": 0.0,
                "reason": "classification error — reviewed manually"}
```

**Why the lexicon-first design matters commercially:** it means the dialect engine
gets *more* accurate over time as your own reviewers correct edge cases into the
lexicon (via the admin endpoint), without needing to retrain a model — a durable
moat that's genuinely hard to copy without the same corpus of corrected examples.

---

## 5. Module: AI Visibility Score (brief spec — build after the two priority modules)

```python
"""app/services/ai_visibility.py — queries generative engines for brand presence."""
import re
from anthropic import Anthropic
# Similarly: openai for ChatGPT, google-generativeai for Gemini, and Perplexity's API.
# Structure is identical across providers: send a category-relevant prompt, parse
# whether/how the brand is mentioned, score sentiment of the mention.

PROMPT_TEMPLATES = [
    "What are the leading companies in {sector} in Nigeria?",
    "Tell me about {brand} and its reputation.",
    "Is {brand} a trustworthy company to do business with?",
    "What are common complaints about {brand}?",
]

def run_visibility_scan(brand: str, sector: str) -> dict:
    results = []
    for template in PROMPT_TEMPLATES:
        prompt = template.format(brand=brand, sector=sector)
        response_text = _query_claude(prompt)  # + _query_chatgpt, _query_gemini, _query_perplexity
        cited = brand.lower() in response_text.lower()
        results.append({"model": "claude", "prompt": prompt, "response_excerpt": response_text[:300],
                        "cited": cited})
    citation_rate = sum(r["cited"] for r in results) / len(results) * 100
    return {"citation_frequency": citation_rate, "results": results}

def _query_claude(prompt: str) -> str:
    # ... same Anthropic client pattern as elsewhere; no system prompt needed here,
    # since you want the model's natural, unprimed answer for an honest visibility read.
    ...
```

Rate-limit this per plan (`ai_visibility_scans` capped monthly per tier) since every
scan is 4 models × N prompts in API spend — expose the cap in
`GET /api/organizations/{id}/limits` so the frontend can show remaining scans.

---

## 6. Module: Multi-User Organizations, Roles & Team Invites

This is the real version of what the frontend currently fakes with `localStorage`.
Every subscriber is an **organization**; every person who can log in is a **member**
of exactly the organizations they've been invited to, with one of three roles:
**owner** (created the account, manages billing and can invite Leads or Members),
**lead** (can invite Members only), and **member** (full workspace access, no invite
or billing rights). This section replaces the demo's plaintext-password-in-`localStorage`
approach with real hashed credentials, invite tokens, and session tokens carrying
organization context for RLS.

### 6.1 Schema

```sql
-- organizations already exists (§1) with plan, workspace_limit, keyword_limit, mention_limit.
-- Add owner/member relationship tables:

CREATE TYPE member_role AS ENUM ('owner', 'lead', 'member');
CREATE TYPE member_status AS ENUM ('invited', 'active', 'suspended');

CREATE TABLE org_members (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  name TEXT NOT NULL,
  password_hash TEXT,                      -- NULL until the invitee sets their own password
  role member_role NOT NULL DEFAULT 'member',
  status member_status NOT NULL DEFAULT 'invited',
  invited_by UUID REFERENCES org_members(id),
  invited_at TIMESTAMPTZ DEFAULT now(),
  activated_at TIMESTAMPTZ,
  last_login_at TIMESTAMPTZ,
  UNIQUE(email)                            -- one person, one organization — matches the
                                            -- "private to the account owner's org alone" requirement
);
CREATE INDEX ix_org_members_org ON org_members(organization_id);

-- Invite tokens: short-lived, single-use, sent by email rather than shown in a toast
CREATE TABLE org_invites (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  member_id UUID NOT NULL REFERENCES org_members(id) ON DELETE CASCADE,
  token TEXT NOT NULL UNIQUE,               -- random 32-byte token, sent as a link, never the raw password
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ
);

-- Session tokens (or swap for signed JWTs if you'd rather stay stateless)
CREATE TABLE sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  member_id UUID NOT NULL REFERENCES org_members(id) ON DELETE CASCADE,
  token TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);

-- RLS: a member can only ever see rows for their own organization
ALTER TABLE org_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY org_members_isolation ON org_members
  USING (organization_id = current_setting('app.current_org_id')::uuid);
```

The `UNIQUE(email)` constraint on `org_members` is deliberate: it enforces "one
email belongs to exactly one organization," which is what makes an account genuinely
private — nobody can accidentally (or maliciously) end up straddling two subscribers'
data with the same login.

### 6.2 Auth service

```python
"""app/services/auth.py — signup, invite, activation, login, role checks."""
from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from passlib.hash import argon2                      # pip install passlib[argon2]
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import Organization, OrgMember, OrgInvite, Session as SessionRow

INVITE_TTL_HOURS = 72
SESSION_TTL_DAYS = 30

def hash_password(raw: str) -> str:
    return argon2.hash(raw)

def verify_password(raw: str, hashed: str) -> bool:
    return bool(hashed) and argon2.verify(raw, hashed)


def create_organization(db: Session, company: str, sector: str, plan: str,
                        owner_email: str, owner_name: str, owner_password: str) -> OrgMember:
    """Signup: creates the organization and its first member (role=owner, active immediately —
    the person just proved they control the email by completing the form on their own session)."""
    if db.scalar(select(OrgMember).where(OrgMember.email == owner_email)):
        raise ValueError("email_already_in_use")
    org = Organization(name=company, sector=sector, plan=plan,
                       workspace_limit={"professional": 1, "corp_growth": 3, "enterprise": None}[plan])
    db.add(org)
    db.flush()
    owner = OrgMember(organization_id=org.id, email=owner_email, name=owner_name,
                      password_hash=hash_password(owner_password), role="owner", status="active",
                      activated_at=datetime.now(timezone.utc))
    db.add(owner)
    db.flush()
    return owner


def invite_member(db: Session, inviter: OrgMember, email: str, name: str, role: str) -> OrgInvite:
    """Enforces the role hierarchy server-side — never trust a client's claimed role."""
    if inviter.role == "member":
        raise PermissionError("members_cannot_invite")
    if inviter.role == "lead" and role != "member":
        raise PermissionError("leads_can_only_invite_members")
    if db.scalar(select(OrgMember).where(OrgMember.email == email)):
        raise ValueError("email_already_in_use")
    member = OrgMember(organization_id=inviter.organization_id, email=email, name=name,
                       role=role, status="invited", invited_by=inviter.id)
    db.add(member)
    db.flush()
    invite = OrgInvite(member_id=member.id, token=secrets.token_urlsafe(32),
                       expires_at=datetime.now(timezone.utc) + timedelta(hours=INVITE_TTL_HOURS))
    db.add(invite)
    db.flush()
    # send_email(email, subject=f"You've been invited to {org.name} on BrandsLens",
    #            body=f"Set your password: https://brandseye.app/accept-invite?token={invite.token}")
    return invite


def accept_invite(db: Session, token: str, password: str) -> OrgMember:
    invite = db.scalar(select(OrgInvite).where(OrgInvite.token == token))
    if not invite or invite.used_at or invite.expires_at < datetime.now(timezone.utc):
        raise ValueError("invite_invalid_or_expired")
    member = db.get(OrgMember, invite.member_id)
    member.password_hash = hash_password(password)
    member.status = "active"
    member.activated_at = datetime.now(timezone.utc)
    invite.used_at = datetime.now(timezone.utc)
    return member


def authenticate(db: Session, email: str, password: str) -> OrgMember:
    member = db.scalar(select(OrgMember).where(OrgMember.email == email, OrgMember.status == "active"))
    if not member or not verify_password(password, member.password_hash):
        raise ValueError("invalid_credentials")
    member.last_login_at = datetime.now(timezone.utc)
    return member


def issue_session(db: Session, member: OrgMember) -> str:
    token = secrets.token_urlsafe(32)
    db.add(SessionRow(member_id=member.id, token=token,
                      expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)))
    return token


def remove_member(db: Session, actor: OrgMember, target_id: str) -> None:
    target = db.get(OrgMember, target_id)
    if target.organization_id != actor.organization_id:
        raise PermissionError("cross_org_action_denied")
    if target.role == "owner":
        raise PermissionError("cannot_remove_owner")
    if actor.role == "member":
        raise PermissionError("members_cannot_remove")
    if actor.role == "lead" and target.role != "member":
        raise PermissionError("leads_can_only_remove_members")
    db.delete(target)
```

### 6.3 API routes

```
POST   /api/auth/signup              { company, sector, plan, name, email, password }
                                      -> creates organization + owner, returns session token

POST   /api/auth/login               { email, password } -> session token
POST   /api/auth/logout              revokes the session token

POST   /api/team/invite              { email, name, role } — role gated server-side per §6.2
GET    /api/team/members             list of the caller's organization only (RLS-enforced)
DELETE /api/team/members/{id}        gated: owner removes lead/member; lead removes member only
POST   /api/invites/{token}/accept   { password } -> activates the invited member, returns session token
GET    /api/invites/{token}          validates a token before rendering the "set your password" page
```

Every non-auth route in the API (incidents, keywords, escalations, scans — everything
from §2 of this document) resolves the caller's `organization_id` from their session
token and sets `app.current_org_id` for that request's transaction, exactly like the
existing `app.current_client_id` pattern in §1 — the two compose naturally, since a
`client` (workspace) row belongs to an `organization`, and RLS on `incidents` already
joins through `clients.organization_id`.

### 6.4 Why this is a meaningful upgrade over the demo

The frontend build you have right now stores plaintext passwords in a browser's
`localStorage`, keyed by a string that only that one browser ever sees. That's fine
for showing a prospect how team roles will work, but it is not real security and
does not work across devices. This section fixes both: passwords are hashed with
Argon2 (a modern, memory-hard hash, not reversible even if the database leaks),
invites are single-use tokens with an expiry rather than a plaintext password shown
in a toast, sessions are server-issued and revocable, and the whole thing lives in a
real database that every team member's device can reach — not one laptop's browser.

---

## 7. Module: Billing — Stripe & Paystack

The one piece with zero backend work started anywhere else in this document.
Two providers because they serve different cards well: **Stripe** for
international cards, **Paystack** for Nigerian cards and bank transfer. Both
follow the same non-negotiable rule: **the browser redirect is never what
grants access — only a verified webhook is.** A customer's browser can close,
lie, or get intercepted; a signed webhook from Stripe or Paystack's own servers
cannot be forged without their signing secret.

### 7.1 Schema

```sql
-- migrations/003_billing.sql
-- Adds billing state to the organizations table (from architecture blueprint §1)
-- and an append-only log of every webhook received, so a payment dispute or a
-- "why did my plan change" support ticket can always be traced back to the
-- exact event that caused it.

ALTER TABLE organizations
  ADD COLUMN billing_provider TEXT,                -- 'stripe' | 'paystack' | NULL (never paid)
  ADD COLUMN billing_customer_id TEXT,             -- Stripe customer id or Paystack customer code
  ADD COLUMN billing_subscription_id TEXT,
  ADD COLUMN billing_status TEXT NOT NULL DEFAULT 'trialing',
    -- 'trialing' | 'active' | 'past_due' | 'cancelled'
  ADD COLUMN plan_activated_at TIMESTAMPTZ,
  ADD COLUMN plan_cancelled_at TIMESTAMPTZ,
  ADD COLUMN read_only_after TIMESTAMPTZ;           -- grace period after cancellation before lockout

CREATE TABLE billing_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID REFERENCES organizations(id),
  provider TEXT NOT NULL,
  event_type TEXT NOT NULL,
  raw TEXT,                                        -- truncated JSON payload, for audit/dispute resolution
  received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_billing_events_org ON billing_events(organization_id);

-- Scheduled job (run daily): freeze any organization past its read_only_after date
-- to prevent silent indefinite free usage after a genuine cancellation.
-- UPDATE clients SET scan_enabled = false
--   WHERE organization_id IN (SELECT id FROM organizations WHERE read_only_after < now());

```

### 7.2 Billing service

```python
"""app/services/billing.py — Stripe (international cards) + Paystack (Nigerian
cards/bank transfer) billing, wired to the plan tiers already defined in the
frontend and the Organization model from the architecture blueprint.

Both providers follow the same shape: create a checkout session server-side,
redirect the customer to it, then trust ONLY the webhook (never the browser
redirect) to actually confirm payment and unlock the plan. This is the
standard, correct pattern — a customer's browser closing, refreshing, or lying
about the redirect URL must never be what grants access.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import stripe  # pip install stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET,
                      PAYSTACK_SECRET_KEY, FRONTEND_ORIGIN)
from ..models import Organization, OrgMember, BillingEvent, now_utc

log = logging.getLogger("billing")
stripe.api_key = STRIPE_SECRET_KEY

# ============ PLAN CATALOG — mirrors PLAN_ANNUAL in the frontend exactly ============
# Stripe price IDs are created once in the Stripe Dashboard (or via API) per plan
# per billing cycle, then pasted here. Paystack uses plan codes the same way.
PLAN_CATALOG = {
    "professional": {
        "annual_usd": 3500, "monthly_usd": 335.42,
        "stripe_price_annual": "price_professional_annual",
        "stripe_price_monthly": "price_professional_monthly",
        "paystack_plan_annual": "PLN_professional_annual",
        "paystack_plan_monthly": "PLN_professional_monthly",
    },
    "corp_growth": {
        "annual_usd": 4500, "monthly_usd": 431.25,
        "stripe_price_annual": "price_corpgrowth_annual",
        "stripe_price_monthly": "price_corpgrowth_monthly",
        "paystack_plan_annual": "PLN_corpgrowth_annual",
        "paystack_plan_monthly": "PLN_corpgrowth_monthly",
    },
    "enterprise": {
        "annual_usd": 5500, "monthly_usd": 527.08,
        "stripe_price_annual": "price_enterprise_annual",
        "stripe_price_monthly": "price_enterprise_monthly",
        "paystack_plan_annual": "PLN_enterprise_annual",
        "paystack_plan_monthly": "PLN_enterprise_monthly",
    },
}


# ==================================================================
# STRIPE
# ==================================================================
def create_stripe_checkout(db: Session, org: Organization, plan: str, cycle: str,
                           customer_email: str) -> str:
    """Returns a Stripe-hosted checkout URL. The frontend redirects the browser
    to this URL directly — never collect card details on your own domain
    unless you're PCI-compliant, which Stripe Checkout lets you avoid entirely."""
    catalog = PLAN_CATALOG[plan]
    price_id = catalog["stripe_price_annual"] if cycle == "annual" else catalog["stripe_price_monthly"]

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=customer_email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{FRONTEND_ORIGIN}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{FRONTEND_ORIGIN}/billing/cancelled",
        client_reference_id=org.id,           # <-- this is how the webhook finds the org again
        metadata={"organization_id": org.id, "plan": plan, "cycle": cycle},
        subscription_data={"metadata": {"organization_id": org.id, "plan": plan}},
    )
    return session.url


def handle_stripe_webhook(db: Session, payload: bytes, sig_header: str) -> dict:
    """Verifies the signature (critical — without this, anyone could POST a fake
    'payment succeeded' event) then reconciles subscription state."""
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        log.warning("Rejected Stripe webhook: bad signature or payload")
        raise PermissionError("invalid_stripe_signature")

    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        org_id = obj.get("client_reference_id") or obj.get("metadata", {}).get("organization_id")
        plan = obj.get("metadata", {}).get("plan")
        _activate_plan(db, org_id, plan, provider="stripe",
                       provider_customer_id=obj.get("customer"),
                       provider_subscription_id=obj.get("subscription"))

    elif etype == "customer.subscription.updated":
        org_id = obj.get("metadata", {}).get("organization_id")
        if obj.get("status") in ("past_due", "unpaid"):
            _flag_payment_issue(db, org_id, "stripe", obj.get("status"))
        elif obj.get("status") == "active":
            _clear_payment_issue(db, org_id)

    elif etype == "customer.subscription.deleted":
        org_id = obj.get("metadata", {}).get("organization_id")
        _downgrade_to_free(db, org_id, reason="subscription_cancelled")

    elif etype == "invoice.payment_failed":
        org_id = obj.get("subscription_details", {}).get("metadata", {}).get("organization_id")
        _flag_payment_issue(db, org_id, "stripe", "payment_failed")

    db.add(BillingEvent(organization_id=org_id if 'org_id' in dir() else None,
                        provider="stripe", event_type=etype, raw=json.dumps(event)[:8000]))
    db.commit()
    return {"received": True}


def create_stripe_portal_link(customer_id: str) -> str:
    """Lets a customer manage/cancel their own subscription without you building
    a cancellation flow yourselves — Stripe hosts this page too."""
    portal = stripe.billing_portal.Session.create(
        customer=customer_id, return_url=f"{FRONTEND_ORIGIN}/billing",
    )
    return portal.url


# ==================================================================
# PAYSTACK
# ==================================================================
PAYSTACK_BASE = "https://api.paystack.co"

def create_paystack_checkout(org: Organization, plan: str, cycle: str,
                             customer_email: str) -> str:
    """Paystack's flow: initialize a transaction server-side, redirect the
    customer to the returned authorization_url. Amount is in kobo (NGN) —
    convert your USD plan price at time of charge, or maintain a parallel
    NGN price list set deliberately (don't just multiply by a floating FX rate
    live, since Paystack settlement and customer expectations both want a
    stable, published Naira price)."""
    catalog = PLAN_CATALOG[plan]
    plan_code = catalog["paystack_plan_annual"] if cycle == "annual" else catalog["paystack_plan_monthly"]

    resp = httpx.post(
        f"{PAYSTACK_BASE}/transaction/initialize",
        headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
        json={
            "email": customer_email,
            "plan": plan_code,                # Paystack Plans handle recurring billing for you
            "metadata": {"organization_id": org.id, "plan": plan, "cycle": cycle},
            "callback_url": f"{FRONTEND_ORIGIN}/billing/success",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return data["authorization_url"]


def verify_paystack_signature(payload: bytes, signature_header: str) -> bool:
    computed = hmac.new(PAYSTACK_SECRET_KEY.encode(), payload, hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature_header or "")


def handle_paystack_webhook(db: Session, payload: bytes, signature_header: str) -> dict:
    if not verify_paystack_signature(payload, signature_header):
        log.warning("Rejected Paystack webhook: bad signature")
        raise PermissionError("invalid_paystack_signature")

    event = json.loads(payload)
    etype = event.get("event")
    data = event.get("data", {})
    org_id = (data.get("metadata") or {}).get("organization_id")
    plan = (data.get("metadata") or {}).get("plan")

    if etype == "charge.success":
        _activate_plan(db, org_id, plan, provider="paystack",
                       provider_customer_id=data.get("customer", {}).get("customer_code"),
                       provider_subscription_id=data.get("plan_object", {}).get("plan_code"))
    elif etype == "subscription.disable":
        _downgrade_to_free(db, org_id, reason="subscription_disabled")
    elif etype == "invoice.payment_failed":
        _flag_payment_issue(db, org_id, "paystack", "payment_failed")

    db.add(BillingEvent(organization_id=org_id, provider="paystack",
                        event_type=etype, raw=json.dumps(event)[:8000]))
    db.commit()
    return {"received": True}


# ==================================================================
# SHARED RECONCILIATION HELPERS
# ==================================================================
def _activate_plan(db: Session, org_id: str, plan: str, provider: str,
                   provider_customer_id: str, provider_subscription_id: str) -> None:
    if not org_id:
        log.error("Payment succeeded but no organization_id in webhook metadata — investigate manually")
        return
    org = db.get(Organization, org_id)
    if not org:
        log.error("Payment succeeded for unknown organization_id=%s", org_id)
        return
    org.plan = plan
    org.billing_provider = provider
    org.billing_customer_id = provider_customer_id
    org.billing_subscription_id = provider_subscription_id
    org.billing_status = "active"
    org.plan_activated_at = now_utc()
    org.workspace_limit = {"professional": 1, "corp_growth": 3, "enterprise": None}[plan]
    db.commit()
    log.info("Activated %s plan for org=%s via %s", plan, org_id, provider)
    # notify_org_owner(org, "Your BrandsLens plan is now active.")


def _flag_payment_issue(db: Session, org_id: str, provider: str, status: str) -> None:
    if not org_id: return
    org = db.get(Organization, org_id)
    if not org: return
    org.billing_status = "past_due"
    db.commit()
    log.warning("Payment issue for org=%s (%s): %s", org_id, provider, status)
    # notify_org_owner(org, "Your last BrandsLens payment failed — please update your card.")


def _clear_payment_issue(db: Session, org_id: str) -> None:
    if not org_id: return
    org = db.get(Organization, org_id)
    if org and org.billing_status == "past_due":
        org.billing_status = "active"
        db.commit()


def _downgrade_to_free(db: Session, org_id: str, reason: str) -> None:
    """Never hard-delete data on cancellation. Freeze the workspace read-only
    and keep it retrievable for a grace period (e.g. 30 days) in case the
    customer resubscribes or it was an accidental cancellation."""
    if not org_id: return
    org = db.get(Organization, org_id)
    if not org: return
    org.billing_status = "cancelled"
    org.plan_cancelled_at = now_utc()
    org.read_only_after = now_utc() + timedelta(days=30)
    db.commit()
    log.info("Org=%s downgraded/cancelled (%s) — read-only after %s", org_id, reason, org.read_only_after)

```

### 7.3 API routes

```python
"""app/routes/billing_routes.py — the three endpoints the frontend and the
payment providers actually talk to. Mount this router in app/main.py:
    from .routes.billing_routes import router as billing_router
    app.include_router(billing_router, prefix="/api/billing")
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_member, require_role          # role check reused from §6 of the blueprint
from ..services import billing

router = APIRouter()


class CheckoutBody(BaseModel):
    plan: str            # "professional" | "corp_growth" | "enterprise"
    cycle: str            # "annual" | "monthly"
    provider: str         # "stripe" | "paystack"


@router.post("/checkout")
def start_checkout(body: CheckoutBody, member=Depends(require_role("owner", "lead")),
                   db: Session = Depends(get_db)) -> dict:
    """Only an Owner or Team Lead can change billing — Members are blocked here
    the same way the frontend already blocks them, but enforced server-side."""
    if body.plan not in billing.PLAN_CATALOG:
        raise HTTPException(422, "Unknown plan")
    org = member.organization
    if body.provider == "stripe":
        url = billing.create_stripe_checkout(db, org, body.plan, body.cycle, member.email)
    elif body.provider == "paystack":
        url = billing.create_paystack_checkout(org, body.plan, body.cycle, member.email)
    else:
        raise HTTPException(422, "provider must be 'stripe' or 'paystack'")
    return {"checkout_url": url}


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        return billing.handle_stripe_webhook(db, payload, sig)
    except PermissionError:
        raise HTTPException(400, "Invalid signature")


@router.post("/webhook/paystack")
async def paystack_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    payload = await request.body()
    sig = request.headers.get("x-paystack-signature", "")
    try:
        return billing.handle_paystack_webhook(db, payload, sig)
    except PermissionError:
        raise HTTPException(400, "Invalid signature")


@router.get("/portal")
def billing_portal(member=Depends(require_role("owner", "lead")),
                   db: Session = Depends(get_db)) -> dict:
    """Returns a link to Stripe's own subscription management page — customer
    can update card, cancel, or view invoices without you building any of it."""
    org = member.organization
    if org.billing_provider != "stripe" or not org.billing_customer_id:
        raise HTTPException(400, "No Stripe subscription on this account")
    return {"portal_url": billing.create_stripe_portal_link(org.billing_customer_id)}

```

### 7.4 Environment variables to add

```
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
PAYSTACK_SECRET_KEY=sk_live_...
FRONTEND_ORIGIN=https://brandseye.app
```

### 7.5 One-time setup outside the code

1. In the Stripe Dashboard, create one **Product** ("BrandsLens") with six
   **Prices**: annual and monthly for each of Professional, Corporate Growth,
   Enterprise. Paste the resulting price IDs into `PLAN_CATALOG` in §7.2.
2. In Paystack, create the matching six **Plans** the same way; paste the plan
   codes into the same catalog.
3. Register both webhook endpoints in each provider's dashboard:
   `https://api.brandseye.app/api/billing/webhook/stripe` and
   `.../webhook/paystack` — this is what lets §7.2's webhook handlers actually
   receive events instead of sitting unused.
4. On the frontend, set `window.BRANDSEYE_BILLING_API` to the backend's URL —
   the checkout buttons already call `startCheckout()` first and only fall
   back to the local demo simulation if that variable is unset, so nothing
   else in the frontend needs to change to go live.

### 7.6 What NOT to do

Don't collect card numbers on your own domain — both Checkout flows above are
hosted by the provider specifically so you never touch a card number and
never need PCI compliance yourselves. Don't trust `metadata` on the client
side for anything security-relevant — it's set server-side in §7.2 precisely
so it can't be tampered with. Don't delete an organization's data on
cancellation — `_downgrade_to_free` above freezes it read-only for a grace
period instead, since "I cancelled by accident" is a far more common support
ticket than anyone expects.

---

## 8. Build order

1. **Media Room + immutable audit** — schema, state machine, SLA sweep job. This is
   the module most exposed to compliance scrutiny, so get the hash-chain right first.
2. **Organizations, members & auth (§6)** — this should land second, not last: every
   other module's RLS policies assume `organization_id` already resolves correctly
   from a real session, so build real auth before you have a second paying customer.
3. **Billing (§7)** — wire this immediately after auth. A trial account with no
   path to actually pay you is a trial account forever.
4. **Regional Dialect Engine** — seed the lexicon tables from your own reviewed
   corpus before launch; the LLM fallback is a safety net, not the primary signal.
5. **Emotion Detection** — a single additional classifier call, can piggyback on
   the existing Claude classification batch (`services/classifier.py`) rather than
   a separate pipeline stage.
6. **AI Visibility Score** — lowest urgency, highest per-scan cost; ship last and
   gate hard behind plan limits from day one.
7. **RLS + organizations/plan enforcement** — should land before any paying
   customer's second workspace, not after.
