"""Re-scoring the mentions saved as WATCH while the AI had no credit."""
import pytest
from sqlalchemy import select

from app.models import Incident, MediaRoomCase
from app.services import classifier, demo_seed, reclassify
from tests.helpers import add_incident, make_customer

FALLBACK = reclassify.FALLBACK_MARK


def scorer(plan):
    """A stand-in classifier: plan maps the item's position in a batch to a severity."""
    def classify_batch(brand, context, items):
        return [{"idx": i["idx"], "severity": plan.get(i["idx"], "WATCH"), "sentiment": "Negative", "tags": ["FRAUD"] if plan.get(i["idx"]) == "HIGH" else [],
                 "lang": "en", "rationale": f"scored {plan.get(i['idx'], 'WATCH')}"} for i in items]
    return classify_batch


def unavailable(brand, context, items):
    return [{"idx": i["idx"], "severity": "WATCH", "sentiment": "Neutral", "tags": [], "lang": "en", "rationale": FALLBACK} for i in items]


@pytest.fixture
def failed_mentions(db):
    _, _, ws = make_customer(db)
    mine = [add_incident(db, ws, f"mention {n}", rationale=FALLBACK, ref=f"R{n}", content_hash=f"h{n}") for n in range(3)]
    return ws, mine


def test_a_dry_run_reports_and_changes_nothing(db, failed_mentions, monkeypatch):
    ws, mine = failed_mentions
    monkeypatch.setattr(classifier, "classify_batch", scorer({0: "HIGH", 1: "MEDIUM"}))
    out = reclassify.run(db, dry_run=True)
    assert out["dry_run"] is True and out["found"] == 3 and out["would_become"] == {"HIGH": 1, "MEDIUM": 1, "WATCH": 1}
    db.expire_all()
    assert all(db.get(Incident, i.id).rationale == FALLBACK and db.get(Incident, i.id).severity == "WATCH" for i in mine)
    assert db.scalars(select(MediaRoomCase)).all() == []


def test_applying_updates_the_mentions_and_opens_cases_for_high_and_medium(db, failed_mentions, monkeypatch):
    ws, mine = failed_mentions
    monkeypatch.setattr(classifier, "classify_batch", scorer({0: "HIGH", 1: "MEDIUM"}))
    out = reclassify.run(db, dry_run=False)
    assert out["became"] == {"HIGH": 1, "MEDIUM": 1, "WATCH": 1}
    db.expire_all()
    sev = sorted(db.get(Incident, i.id).severity for i in mine)
    assert sev == ["HIGH", "MEDIUM", "WATCH"]
    assert all(db.get(Incident, i.id).rationale.startswith("scored") for i in mine)
    assert len(db.scalars(select(MediaRoomCase)).all()) == 2
    assert reclassify.run(db, dry_run=False)["found"] == 0           # nothing is left to fix, so a second run does nothing


def test_it_stops_at_once_if_the_ai_is_still_unavailable(db, failed_mentions, monkeypatch):
    ws, mine = failed_mentions
    monkeypatch.setattr(classifier, "classify_batch", unavailable)
    out = reclassify.run(db, dry_run=False)
    assert out["stopped"] and "ai-check" in out["stopped"] and out["examined"] == 0
    db.expire_all()
    assert all(db.get(Incident, i.id).rationale == FALLBACK for i in mine)


def test_it_never_touches_what_a_person_or_the_ai_already_decided(db, failed_mentions, monkeypatch):
    ws, mine = failed_mentions
    overridden = add_incident(db, ws, "a person changed this", rationale=FALLBACK, severity_overridden=True, ref="RO", content_hash="ho")
    scored = add_incident(db, ws, "already scored by the AI", rationale="Looks like a scam.", severity="HIGH", ref="RS", content_hash="hs")
    monkeypatch.setattr(classifier, "classify_batch", scorer({0: "HIGH", 1: "HIGH", 2: "HIGH"}))
    reclassify.run(db, dry_run=False)
    db.expire_all()
    assert db.get(Incident, overridden.id).rationale == FALLBACK and db.get(Incident, overridden.id).severity == "WATCH"
    assert db.get(Incident, scored.id).rationale == "Looks like a scam."


def test_it_skips_demo_workspaces(db, monkeypatch):
    seeded = demo_seed.seed_demo(db, reset=True)
    ws_id = seeded["accounts"][0]["workspace_id"]
    db.add(Incident(workspace_id=ws_id, ref="D1", title="demo", content_hash="dd", rationale=FALLBACK)); db.commit()
    monkeypatch.setattr(classifier, "classify_batch", scorer({0: "HIGH"}))
    assert reclassify.run(db, dry_run=False)["examined"] == 0


def test_the_limit_is_respected(db, monkeypatch):
    _, _, ws = make_customer(db)
    for n in range(10): add_incident(db, ws, f"m{n}", rationale=FALLBACK, ref=f"R{n}", content_hash=f"h{n}")
    monkeypatch.setattr(classifier, "classify_batch", scorer({}))
    assert reclassify.run(db, dry_run=True, limit=4)["found"] == 4


def test_the_setup_route_is_a_dry_run_unless_told_otherwise(client, db, failed_mentions, monkeypatch):
    ws, mine = failed_mentions
    monkeypatch.setattr(classifier, "classify_batch", scorer({0: "HIGH"}))
    assert client.get("/api/setup/reclassify?secret=wrong").status_code == 403
    r = client.get("/api/setup/reclassify?secret=test-secret")
    assert r.json()["dry_run"] is True and r.json()["would_become"]["HIGH"] == 1
    db.expire_all()
    assert db.get(Incident, mine[0].id).rationale == FALLBACK
    assert client.get("/api/setup/reclassify?secret=test-secret&dry_run=false").json()["dry_run"] is False
    db.expire_all()
    assert sorted(db.get(Incident, i.id).severity for i in mine) == ["HIGH", "WATCH", "WATCH"]   # exactly one became HIGH
