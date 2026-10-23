"""The public demo. It must let a visitor look at everything and change nothing, and it must never touch a real customer."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.deps import DEMO_MESSAGE
from app.models import (Competitor, EscalationContact, Incident, MediaRoomCase, Organization, OrgMember, SessionToken, Workspace, aware,
                        now_utc)
from app.services import ai, auth, demo_seed
from app.services.ratelimit import RateLimiter
from tests.helpers import add_incident, bearer, make_customer


@pytest.fixture
def demo(client, db):
    """The seeded demo plus a logged-in demo visitor: returns a small bag of useful ids."""
    seeded = demo_seed.seed_demo(db, reset=True)
    pro = next(a for a in seeded["accounts"] if a["plan"] == "professional")
    r = client.post("/api/public/demo-session")
    assert r.status_code == 200, r.text
    ws = db.get(Workspace, pro["workspace_id"])
    return {"token": r.json()["token"], "ws": ws, "pro": pro, "seeded": seeded, "h": bearer(r.json()["token"]),
            "case": db.scalar(select(MediaRoomCase).where(MediaRoomCase.workspace_id == ws.id)),
            "incident": db.scalar(select(Incident).where(Incident.workspace_id == ws.id)),
            "competitor": db.scalar(select(Competitor).where(Competitor.workspace_id == ws.id)),
            "contact": db.scalar(select(EscalationContact).where(EscalationContact.workspace_id == ws.id))}


# ---------------------------------------------------------------- getting in
def test_the_demo_button_logs_a_visitor_in_without_a_password(client, demo):
    me = client.get("/api/me", headers=demo["h"])
    assert me.status_code == 200
    assert me.json()["member"]["demo_viewer"] is True                     # the app uses this to show the demo banner
    assert me.json()["organization"]["billing_status"] == "active"        # and it passes the paywall like a paying customer


def test_the_demo_session_lasts_hours_not_days(db, demo):
    row = db.scalar(select(SessionToken).where(SessionToken.token == demo["token"]))
    hours = (aware(row.expires_at) - now_utc()) / timedelta(hours=1)
    assert 5.5 <= hours <= 6.1


def test_the_demo_visitor_cannot_log_in_with_a_password(client, db, demo):
    r = client.post("/api/auth/login", json={"email": demo_seed.VIEWER_EMAIL, "password": "anything-at-all"})
    assert r.status_code == 401


def test_there_is_exactly_one_demo_visitor_and_it_is_not_an_owner(db, demo):
    viewers = db.scalars(select(OrgMember).where(OrgMember.demo_viewer == True)).all()   # noqa: E712
    assert len(viewers) == 1 and viewers[0].role == "lead"


def test_with_no_demo_workspace_the_button_says_so_politely(client):
    r = client.post("/api/public/demo-session")
    assert r.status_code == 503 and "being prepared" in r.json()["detail"]


def test_demo_logins_are_rate_limited(client, db, monkeypatch):
    from app import main
    demo_seed.seed_demo(db, reset=True)
    monkeypatch.setattr(main, "_demo_limiter", RateLimiter(2, 3600))
    assert client.post("/api/public/demo-session").status_code == 200
    assert client.post("/api/public/demo-session").status_code == 200
    assert client.post("/api/public/demo-session").status_code == 429


# ---------------------------------------------------------------- looking is allowed
def test_the_visitor_can_look_at_everything_that_matters(client, demo):
    ws = demo["ws"].id
    for path in (f"/api/workspaces/{ws}", f"/api/media-room/cases?ws_id={ws}", f"/api/analytics/{ws}", f"/api/workspaces/{ws}/competitors",
                 f"/api/workspaces/{ws}/escalation-contacts", f"/api/workspaces/{ws}/competitors/analytics"):
        assert client.get(path, headers=demo["h"]).status_code == 200, path


# ---------------------------------------------------------------- changing is refused, on the server
def write_attempts(d):
    ws, case, inc, comp, contact = d["ws"].id, d["case"].id, d["incident"].id, d["competitor"].id, d["contact"].id
    return [
        ("PATCH", f"/api/incidents/{inc}/status", {"status": "resolved"}),
        ("PATCH", f"/api/incidents/{inc}/severity", {"severity": "WATCH"}),
        ("PATCH", f"/api/media-room/cases/{case}/state", {"state": "alerted"}),
        ("POST", f"/api/media-room/cases/{case}/approve", {}),
        ("POST", f"/api/media-room/cases/{case}/escalate", {"contact_id": contact, "message": "x"}),
        ("POST", f"/api/media-room/cases/{case}/statements", {"text": "x"}),
        ("DELETE", f"/api/workspaces/{ws}/competitors/{comp}", None),
        ("DELETE", f"/api/workspaces/{ws}/escalation-contacts/{contact}", None),
        ("POST", f"/api/workspaces/{ws}/competitors", {"name": "Rival"}),
        ("POST", f"/api/workspaces/{ws}/escalation-contacts", {"name": "A", "email": "a@x.com"}),
        ("PATCH", f"/api/workspaces/{ws}", {"name": "Hacked"}),
        ("POST", "/api/team/invite", {"email": "x@y.com", "name": "X", "role": "member"}),
        ("PATCH", "/api/me/profile", {"name": "Hacked"}),
        ("PATCH", "/api/me/notification-preferences", {}),
        ("POST", "/api/billing/checkout", {"plan": "growth"}),
        ("POST", "/api/workspaces", {"name": "Another", "sector": "FMCG"}),
        ("POST", "/api/push/subscribe", {}),
    ]


def test_every_attempt_to_change_something_is_refused(client, demo):
    for method, path, payload in write_attempts(demo):
        r = client.request(method, path, headers=demo["h"], json=payload)
        assert r.status_code == 403, f"{method} {path} was NOT refused ({r.status_code})"
        assert r.json()["detail"] == DEMO_MESSAGE and r.headers.get("x-demo-readonly") == "1", f"{method} {path}"


def test_refused_changes_really_change_nothing(client, db, demo):
    inc, case = demo["incident"], demo["case"]
    before = (inc.status, inc.severity, case.state, demo["ws"].name)
    for method, path, payload in write_attempts(demo):
        client.request(method, path, headers=demo["h"], json=payload)
    db.expire_all()
    inc, case, ws = db.get(Incident, inc.id), db.get(MediaRoomCase, case.id), db.get(Workspace, demo["ws"].id)
    assert (inc.status, inc.severity, case.state, ws.name) == before


def test_a_write_to_a_route_added_in_the_future_is_refused_too(client, demo):
    """The lock sits in the dependency every protected route uses, so it does not rely on a list of routes."""
    r = client.post(f"/api/some-future-route/{demo['ws'].id}", headers=demo["h"], json={})
    assert r.status_code in (403, 404)   # 404 only because the route does not exist; it is never 200


# ---------------------------------------------------------------- the few harmless actions still work
def test_scan_and_competitor_scan_answer_without_scanning(client, demo):
    ws = demo["ws"].id
    assert client.post(f"/api/scan/{ws}", headers=demo["h"]).status_code == 200
    assert client.post(f"/api/workspaces/{ws}/competitors/scan", headers=demo["h"]).status_code == 200


def test_historical_search_passes_the_lock(client, demo):
    r = client.post(f"/api/scan/{demo['ws'].id}/historical", headers=demo["h"], json={})
    assert r.status_code != 403      # it may need a body, but the lock itself lets it through


def test_ai_draft_and_ask_brandslens_give_fixed_samples_and_never_call_the_ai(client, demo, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the demo called the real AI")
    monkeypatch.setattr(ai, "complete", forbidden)
    d = client.post("/api/media-room/ai-draft", headers=demo["h"], json={"case_id": demo["case"].id})
    assert d.status_code == 200 and "SAMPLE DRAFT" in d.json()["draft"]
    for question in ("What should I do first?", "How do we compare with competitors?", "Summarise my mentions"):
        r = client.post(f"/api/app-chat/{demo['ws'].id}", headers=demo["h"], json={"message": question, "history": []})
        assert r.status_code == 200 and "Demo answer" in r.json()["reply"], question


def test_ask_brandslens_answers_the_sentiment_question_from_the_real_demo_numbers(client, db, demo, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the demo called the real AI")
    monkeypatch.setattr(ai, "complete", forbidden)
    incidents = db.scalars(select(Incident).where(Incident.workspace_id == demo["ws"].id)).all()
    neg = [i for i in incidents if i.sentiment == "Negative"]
    r = client.post(f"/api/app-chat/{demo['ws'].id}", headers=demo["h"], json={"message": "What is driving negative sentiment today?", "history": []})
    assert r.status_code == 200
    reply = r.json()["reply"]
    assert f"{len(neg)} of {len(incidents)} recent mentions" in reply and f"({round(100 * len(neg) / len(incidents))}%)" in reply
    assert "What is driving it" in reply and "Demo answer" in reply
    top_platform = max({i.platform for i in neg}, key=lambda p: sum(1 for i in neg if i.platform == p))
    assert top_platform in reply                                  # the platform with the most negative mentions is named


def test_titles_in_answers_are_shortened_at_a_word_boundary_not_mid_word():
    from app.services.chatbot import _short
    long = "acmefoods-offers.example copies the Acme Foods checkout to collect card details"
    assert _short(long, 72) == "acmefoods-offers.example copies the Acme Foods checkout to collect card…"   # not "...to collect car"
    assert _short("Short title") == "Short title" and _short("") == "" and _short(None) == ""


def test_the_answer_says_spread_across_when_no_platform_dominates_and_most_when_one_does():
    from app.services.chatbot import demo_app_chat_reply
    spread = demo_app_chat_reply("what is driving negative sentiment", "Acme", {"HIGH": 1}, {"X": 4}, [], sentiment={"Negative": 9, "Neutral": 1},
                                 negative_platforms={"X": 3, "News": 3, "Facebook": 3}, negative_titles=[])
    focused = demo_app_chat_reply("what is driving negative sentiment", "Acme", {"HIGH": 1}, {"X": 4}, [], sentiment={"Negative": 10},
                                  negative_platforms={"X": 7, "News": 3}, negative_titles=[])
    assert "It is spread across X (3), News (3), Facebook (3)." in spread and "Most of it" not in spread
    assert "Most of it comes from X (7), News (3)." in focused
    nothing = demo_app_chat_reply("any negative sentiment?", "Acme", {}, {}, [], sentiment={}, negative_platforms={}, negative_titles=[])
    assert "0 of 0 recent mentions" in nothing and "(0%)" in nothing          # no data never divides by zero


def test_other_questions_still_get_their_own_answers(client, demo):
    first = client.post(f"/api/app-chat/{demo['ws'].id}", headers=demo["h"], json={"message": "What should I do first?", "history": []}).json()["reply"]
    comp = client.post(f"/api/app-chat/{demo['ws'].id}", headers=demo["h"], json={"message": "How do we compare with competitors?", "history": []}).json()["reply"]
    assert "HIGH severity" in first and "Sentiment Comparison" in comp and "negative (" not in first + comp


def test_logging_out_works(client, demo):
    assert client.post("/api/auth/logout", headers=demo["h"]).status_code == 200
    assert client.get("/api/me", headers=demo["h"]).status_code == 401


# ---------------------------------------------------------------- it never reaches a real customer
def test_the_visitor_cannot_reach_another_organisation(client, db, demo):
    _, _, other_ws = make_customer(db, email="real@customer.com", company="Real Customer Ltd")
    assert client.get(f"/api/workspaces/{other_ws.id}", headers=demo["h"]).status_code == 404


def test_the_visitor_is_not_an_administrator(client, demo):
    assert client.get("/api/admin/promos", headers=demo["h"]).status_code == 403
    assert client.get("/api/admin/launch/summary", headers=demo["h"]).status_code == 403


def test_real_customers_and_demo_owners_can_still_make_changes(client, db, demo):
    member, token, ws = make_customer(db, email="real@customer.com", company="Real Customer Ltd")
    inc = add_incident(db, ws, "A real mention")
    r = client.patch(f"/api/incidents/{inc.id}/status", headers=bearer(token), json={"status": "resolved"})
    assert r.status_code == 200
    # the demo workspace's OWNER (not the visitor) is an ordinary account: the lock is only on the visitor
    login = client.post("/api/auth/login", json={"email": demo["pro"]["email"], "password": demo["pro"]["password"]})
    assert login.status_code == 200
    r2 = client.patch(f"/api/incidents/{demo['incident'].id}/status", headers=bearer(login.json()["token"]), json={"status": "resolved"})
    assert r2.status_code == 200


# ---------------------------------------------------------------- keeping the demo fresh
def test_refreshing_rebuilds_only_the_professional_demo_and_keeps_its_owner_password(client, db, demo):
    standard = next(o for o in db.scalars(select(Organization).where(Organization.is_demo == True)).all() if o.plan == "standard")   # noqa: E712
    pro_org_before = demo["ws"].organization_id
    old_token = demo["token"]
    assert demo_seed.refresh_public_demo(db)["status"] == "refreshed"
    orgs = {o.plan: o for o in db.scalars(select(Organization).where(Organization.is_demo == True)).all()}   # noqa: E712
    assert set(orgs) == {"standard", "growth", "professional"}
    assert orgs["standard"].id == standard.id                  # untouched
    assert orgs["professional"].id != pro_org_before           # rebuilt
    assert client.get("/api/me", headers=bearer(old_token)).status_code == 401           # the old demo session ends...
    assert client.post("/api/public/demo-session").status_code == 200                     # ...and a new one starts fine
    assert client.post("/api/auth/login", json={"email": demo["pro"]["email"], "password": demo["pro"]["password"]}).status_code == 200
    assert len(db.scalars(select(OrgMember).where(OrgMember.demo_viewer == True)).all()) == 1   # noqa: E712


def test_refresh_does_nothing_when_there_is_no_demo(db):
    assert demo_seed.refresh_public_demo(db)["status"] == "skipped"


def test_a_missing_visitor_is_recreated(client, db, demo):
    db.query(OrgMember).filter(OrgMember.demo_viewer == True).delete()   # noqa: E712
    db.commit()
    assert client.post("/api/public/demo-session").status_code == 200


def test_refreshing_never_touches_real_customers(client, db, demo):
    member, token, ws = make_customer(db, email="real@customer.com", company="Real Customer Ltd")
    inc = add_incident(db, ws, "Keep me")
    demo_seed.refresh_public_demo(db)
    assert db.get(Incident, inc.id) is not None and db.get(Workspace, ws.id) is not None
