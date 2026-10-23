"""The visit tracker, the code preview, the launch report and the admin tools."""
import pytest
from sqlalchemy import select

from app.models import LaunchEvent, PromoCode, SignupAttribution
from app.services import promos
from app.services.ratelimit import RateLimiter
from tests.helpers import bearer, make_admin, make_customer, seed_valid_codes

OK_EVENT = {"kind": "visit", "session_id": "sess_abcdef123456", "utm_source": "producthunt", "utm_medium": "launch",
            "utm_campaign": "official_launch", "path": "/launch"}


# ---------------------------------------------------------------- the tracker
def test_a_visit_is_recorded_anonymously(client, db):
    assert client.post("/api/public/track", json=OK_EVENT).status_code == 204
    e = db.scalar(select(LaunchEvent))
    assert (e.kind, e.utm_source, e.utm_campaign, e.path, e.session_id) == ("visit", "producthunt", "official_launch", "/launch", "sess_abcdef123456")
    assert not any(c.name in ("ip", "ip_address", "user_agent") for c in LaunchEvent.__table__.columns)   # nothing personal is even possible to store


@pytest.mark.parametrize("bad", [
    {**OK_EVENT, "kind": "hack"}, {**OK_EVENT, "session_id": "x"}, {**OK_EVENT, "session_id": "has spaces and <tags>!!"},
    {**OK_EVENT, "path": "javascript:alert(1)"}, {**OK_EVENT, "kind": ""}])
def test_junk_is_quietly_ignored_and_stores_nothing(client, db, bad):
    assert client.post("/api/public/track", json=bad).status_code == 204
    assert db.scalars(select(LaunchEvent)).all() == []


def test_tags_and_paths_are_cleaned_before_storing(client, db):
    ev = {**OK_EVENT, "utm_source": "Product Hunt!<b>", "path": "/launch?utm_source=x&secret=1#frag"}
    client.post("/api/public/track", json=ev)
    e = db.scalar(select(LaunchEvent))
    assert e.utm_source == "producthuntb" and e.path == "/launch"


def test_the_tracker_is_rate_limited(client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_track_limiter", RateLimiter(3, 600))
    assert [client.post("/api/public/track", json=OK_EVENT).status_code for _ in range(5)] == [204, 204, 204, 429, 429]


# ---------------------------------------------------------------- the code preview shown at checkout
def test_preview_shows_the_discount_for_a_valid_code(client, db):
    seed_valid_codes(db)
    r = client.get("/api/public/promo/ph15?plan=growth&cycle=annual&quantity=1").json()
    assert r["valid"] is True and r["percent"] == 15 and r["discount"] > 0 and r["total"] == round(r["full_total"] - r["discount"], 2)


@pytest.mark.parametrize("url,fragment", [
    ("/api/public/promo/NOPE?plan=growth&cycle=annual", "isn't valid"),
    ("/api/public/promo/PH15?plan=growth&cycle=monthly", "annual"),
    ("/api/public/promo/PH15?plan=enterprise&cycle=annual", "Standard, Growth and Professional"),
    ("/api/public/promo/PH15?plan=growth&cycle=annual&quantity=99", "between 1 and"),
    ("/api/public/promo/PH15?plan=growth&cycle=weekly", "annual, monthly or daily")])
def test_preview_always_answers_with_a_plain_reason(client, db, url, fragment):
    seed_valid_codes(db)
    r = client.get(url)
    assert r.status_code == 200 and r.json()["valid"] is False and fragment in r.json()["message"]


def test_preview_is_rate_limited(client, db, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_promo_limiter", RateLimiter(2, 600))
    assert [client.get("/api/public/promo/PH15").status_code for _ in range(3)] == [200, 200, 429]


# ---------------------------------------------------------------- the launch report
def test_the_launch_report_follows_each_platform_from_visit_to_payment(db):
    def ev(kind, sess, src): db.add(LaunchEvent(kind=kind, session_id=sess, utm_source=src))
    for s in ("a1", "a2", "a3"): ev("visit", s + "xxxxxx", "producthunt")
    ev("visit", "a1xxxxxx", "producthunt")                 # the same visitor again: still one visitor
    ev("demo_start", "a1xxxxxx", "producthunt"); ev("demo_start", "a2xxxxxx", "producthunt")
    ev("visit", "h1xxxxxx", "hackernews"); ev("visit", "d1xxxxxx", "")
    db.add_all([
        SignupAttribution(pending_signup_id="p1", utm_source="producthunt", promo_code="PH15", status="paid", currency="USD", charged_amount=1275, discount_amount=225),
        SignupAttribution(pending_signup_id="p2", utm_source="producthunt", promo_code="PH15", status="started", currency="USD", charged_amount=1275),
        SignupAttribution(pending_signup_id="p3", utm_source="hackernews", promo_code="HN15", status="paid", currency="NGN", charged_amount=2000000, discount_amount=350000)])
    db.commit()
    rep = promos.launch_summary(db)
    ph = next(r for r in rep["sources"] if r["source"] == "producthunt")
    assert (ph["visitors"], ph["demo_starts"], ph["checkouts_started"], ph["paid"]) == (3, 2, 2, 1)
    assert ph["revenue"] == {"USD": 1275.0} and ph["visit_to_paid_percent"] == 33.3
    hn = next(r for r in rep["sources"] if r["source"] == "hackernews")
    assert hn["revenue"] == {"NGN": 2000000.0}                              # currencies are never added together
    assert any(r["source"] == "(direct)" for r in rep["sources"])
    codes = {c["code"]: c for c in rep["codes"]}
    assert codes["PH15"]["paid"] == 1 and codes["PH15"]["checkouts_started"] == 2 and codes["PH15"]["discount_given"] == {"USD": 225.0}
    assert rep["totals"]["paid"] == 2 and rep["sources"][0]["source"] in ("producthunt", "hackernews")


def test_an_empty_report_is_fine(db):
    assert promos.launch_summary(db) == {"sources": [], "campaigns": [], "codes": [], "totals": {"visitors": 0, "demo_starts": 0, "checkouts_started": 0, "paid": 0}}


# ---------------------------------------------------------------- the admin tools
def admin_headers(client, db):
    make_admin(db)
    return bearer(client.post("/api/auth/login", json={"email": "admin@brandslens.test", "password": "admin-pass-123"}).json()["token"])


def test_only_the_brandslens_team_can_manage_codes(client, db):
    _, token, _ = make_customer(db)
    for method, path in (("GET", "/api/admin/promos"), ("POST", "/api/admin/promos"), ("GET", "/api/admin/launch/summary")):
        assert client.request(method, path, headers=bearer(token), json={"code": "EVIL50"}).status_code == 403
    assert client.get("/api/admin/promos").status_code == 401


def test_the_team_can_create_edit_and_switch_off_a_code(client, db):
    h = admin_headers(client, db)
    made = client.post("/api/admin/promos", headers=h, json={"code": " lt-15 ", "percent": 15, "platform": "LinkedIn", "valid_from": "2026-11-24", "valid_until": "2026-12-24", "max_redemptions": 50})
    assert made.status_code == 200 and made.json()["code"] == "LT15"
    assert made.json()["valid_from"].startswith("2026-11-23T23:00")          # a bare date is midnight Lagos time
    assert made.json()["valid_until"].startswith("2026-12-24T22:59")         # an end date runs to the end of that day, Lagos time
    pid = made.json()["id"]
    assert client.post("/api/admin/promos", headers=h, json={"code": "lt15"}).status_code == 409
    upd = client.patch(f"/api/admin/promos/{pid}", headers=h, json={"percent": 20, "active": False})
    assert upd.status_code == 200 and upd.json()["percent"] == 20 and upd.json()["active"] is False
    with pytest.raises(promos.PromoError):                                   # switched off: nobody can use it
        promos.get_valid(db, "LT15", "growth", "annual")
    assert client.patch(f"/api/admin/promos/{pid}", headers=h, json={"max_redemptions": None}).json()["max_redemptions"] is None
    listing = client.get("/api/admin/promos", headers=h).json()["promos"]
    assert [p["code"] for p in listing] == ["LT15"] and listing[0]["paid_redemptions"] == 0


@pytest.mark.parametrize("payload,status", [({"code": "ab"}, 422), ({"code": "THIS-CODE-IS-FAR-TOO-LONG-TO-BE-VALID-ok"}, 422),
                                            ({"code": "OK15", "percent": 0}, 422), ({"code": "OK15", "percent": 95}, 422),
                                            ({"code": "OK15", "valid_from": "next tuesday"}, 422)])
def test_bad_codes_are_refused(client, db, payload, status):
    assert client.post("/api/admin/promos", headers=admin_headers(client, db), json=payload).status_code == status


def test_the_launch_report_is_available_to_the_team(client, db):
    h = admin_headers(client, db)
    db.add(LaunchEvent(kind="visit", session_id="zzzzzzzz1", utm_source="peerlist")); db.commit()
    r = client.get("/api/admin/launch/summary?days=30", headers=h)
    assert r.status_code == 200 and r.json()["sources"][0]["source"] == "peerlist"


def test_the_setup_route_creates_the_codes_and_needs_the_secret(client, db):
    assert client.get("/api/setup/seed-launch-codes?secret=wrong").status_code == 403
    r = client.get("/api/setup/seed-launch-codes?secret=test-secret")
    assert r.status_code == 200 and sorted(r.json()["created"]) == ["HN15", "IH15", "PEERLIST15", "PH15"] and r.json()["percent"] == 15
    assert client.get("/api/setup/seed-launch-codes?secret=test-secret&starts_on=nonsense").status_code == 422
    assert db.scalar(select(PromoCode).where(PromoCode.code == "PH15")).valid_from is not None
