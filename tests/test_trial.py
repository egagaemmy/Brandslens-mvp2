"""The free trial: business-email access, an exact clock, caps, the team's controls, the lead export and the nurture sequence."""
import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Incident, Organization, OrgMember, TrialEmail, TrialSignup, Workspace, now_utc
from app.services import auth, billing, chatbot, mailer, media_room, trial
from tests.helpers import add_incident, bearer, make_admin, make_customer

PASSWORD = "correct-horse-1"


@pytest.fixture
def mails(monkeypatch):
    sent = []

    def fake(to, subject, html_body, from_addr="", reply_to=""):
        sent.append({"to": to, "subject": subject, "html": html_body, "from": from_addr, "reply_to": reply_to})
        return True
    monkeypatch.setattr(mailer, "send_email", fake)
    return sent


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """The limiters live in memory for the life of the process, so each test starts with them empty."""
    from app import routes_trial
    for limiter in (routes_trial._start_ip, routes_trial._start_email, routes_trial._verify_ip, routes_trial._request_ip):
        limiter._hits.clear()
    yield


@pytest.fixture
def admin(client, db):
    make_admin(db)
    return bearer(client.post("/api/auth/login", json={"email": "admin@brandslens.test", "password": "admin-pass-123"}).json()["token"])


def open_trials(db, **extra):
    return trial.save_settings(db, "test", {"enabled": True, **extra})


def begin(client, email="ada@acme-foods.com", name="Ada Okafor", company="Acme Foods", consent=True, **extra):
    return client.post("/api/public/trial/start", json={"email": email, "name": name, "company": company, "marketing_consent": consent, **extra})


def code_for(mails, email):
    mail = next(m for m in reversed(mails) if m["to"] == email and "verification code" in m["subject"])
    return re.search(r">(\d{6})<", mail["html"]).group(1)


def start_trial(client, db, mails, email="ada@acme-foods.com", company="Acme Foods", consent=True, **extra):
    open_trials(db) if not trial.get_settings(db)["enabled"] else None
    assert begin(client, email=email, company=company, consent=consent, **extra).status_code == 200
    r = client.post("/api/public/trial/verify", json={"email": email, "code": code_for(mails, email), "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


def ws_of(db, email="ada@acme-foods.com"):
    """That trial's own workspace (the team account has one of its own, so a bare select would pick the wrong one)."""
    return db.scalar(select(Workspace).where(Workspace.organization_id == org_of(db, email).id))


def org_of(db, email):
    return db.get(Organization, db.scalar(select(OrgMember).where(OrgMember.email == email)).organization_id)


# ---------------------------------------------------------------- who may start: business email only
@pytest.mark.parametrize("email", ["a@gmail.com", "A@Gmail.COM", "a@yahoo.com", "a@yahoo.co.uk", "a@ymail.com", "a@outlook.com", "a@hotmail.co.uk", "a@live.com",
                                   "a@icloud.com", "a@protonmail.com", "a@proton.me", "a@gmx.de", "a@aol.com", "a@mail.ru", "a@yandex.ru", "a@mail.gmail.com"])
def test_personal_email_providers_are_refused(email):
    ok, why = trial.is_business_email(email)
    assert not ok and "work email" in why


@pytest.mark.parametrize("email", ["a@mailinator.com", "a@guerrillamail.net", "a@10minutemail.com", "a@yopmail.com", "a@sub.mailinator.com"])
def test_throwaway_addresses_are_refused(email):
    ok, why = trial.is_business_email(email)
    assert not ok and "Temporary" in why


@pytest.mark.parametrize("email", ["", "no-at-sign", "a@b", "a b@c.com", "a@@b.com", "a@b..com", "<x>@acme.com", "a@" + "b" * 260 + ".com"])
def test_malformed_addresses_are_refused(email):
    assert trial.is_business_email(email)[0] is False


@pytest.mark.parametrize("email", ["ada@acme-foods.com", "jo@company.com.ng", "a@sub.example.co.uk", "Ada@Acme-Foods.COM", "x@livestock.com", "x@outlookconsulting.com",
                                   "x@protonlabs.io", "x@msnbc.com", "x@yahooventures.com", "x@gmxsolutions.com", "x@gmail.com.acme.com", "x@notgmail.com"])
def test_real_company_addresses_are_accepted_including_names_that_merely_resemble_a_provider(email):
    assert trial.is_business_email(email) == (True, "")


# ---------------------------------------------------------------- the controls and when the trial is open
def test_trials_are_off_until_the_team_switches_them_on(client, db, mails):
    s = trial.get_settings(db)
    assert s["enabled"] is False and s["days"] == 30 and s["daily_cap"] == 50
    assert s["caps"] == {"workspaces": 1, "keywords": 5, "members": 3, "competitors": 3, "historical_days": 30, "ai_drafts": 10, "ask_messages": 25}
    r = begin(client)
    assert r.status_code == 503 and "aren't open" in r.json()["detail"] and mails == []
    assert client.get("/api/public/trial/status").json()["open"] is False


def test_settings_accept_good_values_and_refuse_bad_ones(db):
    out = trial.save_settings(db, "t", {"enabled": True, "days": 14, "daily_cap": 20, "stop_date": "2027-02-01", "offer_code": " trial-15! ", "caps": {"keywords": 8, "ai_drafts": 4}})
    assert out["days"] == 14 and out["daily_cap"] == 20 and out["stop_date"] == "2027-02-01" and out["offer_code"] == "TRIAL-15"
    assert out["caps"]["keywords"] == 8 and out["caps"]["ai_drafts"] == 4 and out["caps"]["members"] == 3
    for bad in ({"days": 0}, {"days": 181}, {"days": "30"}, {"days": True}, {"daily_cap": -1}, {"enabled": "yes"}, {"stop_date": "tomorrow"},
                {"caps": {"nonsense": 1}}, {"caps": {"keywords": 0}}, {"caps": {"keywords": 51}}, {"caps": {"workspaces": 4}}, {"caps": {"ai_drafts": "5"}}):
        with pytest.raises(trial.TrialError):
            trial.save_settings(db, "t", bad)
    assert trial.get_settings(db)["days"] == 14


def test_the_programme_closes_on_its_stop_date_and_at_the_daily_limit(client, db, mails):
    now = now_utc()
    open_trials(db, stop_date=(now - timedelta(days=1)).strftime("%Y-%m-%d"))
    assert trial.accepting(db, now) == (False, "The free trial offer has closed.")
    open_trials(db, stop_date=now.strftime("%Y-%m-%d"))
    assert trial.accepting(db, now)[0] is True                                   # the stop date itself is the last open day
    open_trials(db, stop_date="", daily_cap=1)
    start_trial(client, db, mails)
    ok, why = trial.accepting(db, now_utc())
    assert not ok and "today's limit" in why
    assert begin(client, email="bola@other-company.com", company="Other Company").status_code == 503


# ---------------------------------------------------------------- starting and verifying
def test_a_personal_email_is_refused_with_a_clear_reason_and_no_email_is_sent(client, db, mails):
    open_trials(db)
    r = begin(client, email="ada@gmail.com")
    assert r.status_code == 422 and "work email" in r.json()["detail"] and mails == []


def test_the_code_email_comes_from_the_trial_sender_and_replies_go_to_the_real_inbox(client, db, mails):
    open_trials(db)
    assert begin(client).status_code == 200
    m = mails[0]
    assert m["to"] == "ada@acme-foods.com" and m["from"] == "Pat from BrandsLens <info@mail.brandslens.com>" and m["reply_to"] == "info@brandslens.com"
    assert re.search(r">\d{6}<", m["html"])
    row = db.scalar(select(TrialSignup))
    assert row.status == "pending" and len(row.code_hash) == 64 and code_for(mails, "ada@acme-foods.com") not in row.code_hash


def test_asking_for_another_code_too_soon_is_refused_but_works_after_a_minute(client, db, mails):
    open_trials(db)
    assert begin(client).status_code == 200
    r = begin(client)
    assert r.status_code == 429 and "wait a minute" in r.json()["detail"]
    trial.start(db, email="ada@acme-foods.com", name="Ada Okafor", company="Acme Foods", consent=True, now=now_utc() + timedelta(seconds=61))


def test_a_wrong_code_a_short_password_and_an_expired_code_are_all_refused(client, db, mails):
    open_trials(db)
    begin(client)
    good = code_for(mails, "ada@acme-foods.com")
    bad = "000000" if good != "000000" else "111111"
    assert client.post("/api/public/trial/verify", json={"email": "ada@acme-foods.com", "code": bad, "password": PASSWORD}).status_code == 422
    short = client.post("/api/public/trial/verify", json={"email": "ada@acme-foods.com", "code": good, "password": "short"})
    assert short.status_code == 422 and "8 characters" in short.json()["detail"]
    assert db.scalar(select(TrialSignup)).code_attempts == 1                    # the short password did not use up an attempt
    row = db.scalar(select(TrialSignup))
    row.code_expires_at = now_utc() - timedelta(seconds=1)
    db.commit()
    r = client.post("/api/public/trial/verify", json={"email": "ada@acme-foods.com", "code": good, "password": PASSWORD})
    assert r.status_code == 422 and "expired" in r.json()["detail"]


def test_five_wrong_guesses_lock_the_code_even_for_the_right_answer(client, db, mails):
    open_trials(db)
    begin(client)
    good = code_for(mails, "ada@acme-foods.com")
    bad = "000000" if good != "000000" else "111111"
    for _ in range(5):
        client.post("/api/public/trial/verify", json={"email": "ada@acme-foods.com", "code": bad, "password": PASSWORD})
    r = client.post("/api/public/trial/verify", json={"email": "ada@acme-foods.com", "code": good, "password": PASSWORD})
    assert r.status_code == 429 and "request a new code" in r.json()["detail"]


def test_verifying_creates_a_capped_professional_trial_that_ends_exactly_30_days_later(client, db, mails):
    out = start_trial(client, db, mails)
    org = org_of(db, "ada@acme-foods.com")
    assert org.billing_status == "trialing" and org.plan == "professional" and org.workspace_limit == 1 and org.keyword_limit == 5
    assert abs((org.trial_ends_at - org.trial_started_at) - timedelta(days=30)) < timedelta(seconds=2)
    row = db.scalar(select(TrialSignup))
    assert row.status == "active" and row.ends_at == org.trial_ends_at and row.organization_id == org.id and row.code_hash == ""
    ws = db.scalar(select(Workspace).where(Workspace.organization_id == org.id))
    assert len(ws.keywords) == 5
    h = bearer(out["token"])
    assert client.get(f"/api/workspaces/{ws.id}", headers=h).status_code == 200
    me = client.get("/api/trial/me", headers=h).json()
    assert me["is_trial"] is True and me["days_left"] == 30 and me["limits"]["keywords"] == 5 and me["usage"] == {"ai_drafts": 0, "ask_messages": 0}


def test_the_team_hears_about_every_new_lead_and_the_person_gets_a_welcome(client, db, mails):
    start_trial(client, db, mails, utm_source="producthunt", utm_medium="launch", utm_campaign="official_launch")
    notice = next(m for m in mails if m["to"] == "info@brandslens.com")
    assert "Acme Foods" in notice["subject"] and "producthunt / launch / official_launch" in notice["html"] and notice["reply_to"] == "ada@acme-foods.com"
    welcome = next(m for m in mails if m["to"] == "ada@acme-foods.com" and "trial is live" in m["subject"])
    assert welcome["from"] == "Pat from BrandsLens <info@mail.brandslens.com>" and "{{" not in welcome["html"]


def test_campaign_tags_and_consent_are_recorded_as_given(client, db, mails):
    start_trial(client, db, mails, utm_source="Product Hunt!", utm_campaign="Official_Launch", utm_term="x" * 200, consent=False)
    row = db.scalar(select(TrialSignup))
    assert (row.utm_source, row.utm_campaign) == ("producthunt", "official_launch") and len(row.utm_term) == 80
    assert row.marketing_consent is False and row.consent_at is None


def test_one_trial_per_email_and_one_per_company_per_year(client, db, mails):
    start_trial(client, db, mails)
    again = begin(client)
    assert again.status_code == 409 and "already exists" in again.json()["detail"] and "log in" in again.json()["detail"]
    colleague = begin(client, email="bola@acme-foods.com", name="Bola Ade")
    assert colleague.status_code == 409 and "acme-foods.com" in colleague.json()["detail"] and "Team settings" in colleague.json()["detail"]
    row = db.scalar(select(TrialSignup))
    row.started_at = now_utc() - timedelta(days=400)
    db.commit()
    assert begin(client, email="bola@acme-foods.com", name="Bola Ade").status_code == 200


def test_an_email_that_already_has_an_account_cannot_start_a_trial(client, db, mails):
    open_trials(db)
    make_customer(db, email="boss@real-customer.com")
    r = begin(client, email="boss@real-customer.com", company="Real Customer")
    assert r.status_code == 409 and "already exists" in r.json()["detail"]


def test_a_blocked_address_cannot_start_a_trial(client, db, mails):
    open_trials(db)
    db.add(TrialSignup(email="x@bad-actor.com", domain="bad-actor.com", status="blocked"))
    db.commit()
    assert begin(client, email="x@bad-actor.com", company="Bad Actor").status_code == 403


def test_the_starter_keywords_never_exceed_the_cap(client, db, mails):
    open_trials(db, caps={"keywords": 3})
    start_trial(client, db, mails)
    assert len(ws_of(db).keywords) == 3 and org_of(db, "ada@acme-foods.com").keyword_limit == 3


def test_the_length_is_locked_when_a_trial_starts_and_a_later_change_only_affects_new_trials(client, db, mails):
    start_trial(client, db, mails)
    first = org_of(db, "ada@acme-foods.com").trial_ends_at
    trial.save_settings(db, "t", {"days": 14})
    assert org_of(db, "ada@acme-foods.com").trial_ends_at == first
    start_trial(client, db, mails, email="jo@other-company.com", company="Other Company")
    o2 = org_of(db, "jo@other-company.com")
    assert abs((o2.trial_ends_at - o2.trial_started_at) - timedelta(days=14)) < timedelta(seconds=2)


# ---------------------------------------------------------------- the exact clock
def test_access_stops_at_the_stored_moment_not_at_the_next_nightly_job(client, db, mails):
    out = start_trial(client, db, mails)
    h, org = bearer(out["token"]), org_of(db, "ada@acme-foods.com")
    ws = db.scalar(select(Workspace).where(Workspace.organization_id == org.id))
    org.trial_ends_at = now_utc() + timedelta(seconds=30)
    db.commit()
    assert client.get(f"/api/workspaces/{ws.id}", headers=h).status_code == 200            # still inside the trial
    org.trial_ends_at = now_utc() - timedelta(seconds=1)
    db.commit()
    r = client.get(f"/api/workspaces/{ws.id}", headers=h)
    assert r.status_code == 402 and r.headers["X-Trial-Expired"] == "1" and "free trial has ended" in r.json()["detail"]
    db.expire_all()
    assert db.get(Organization, org.id).billing_status == "unpaid" and db.scalar(select(TrialSignup)).status == "expired"


def test_a_trialing_organization_with_no_end_date_is_locked_out_never_open_ended(client, db, mails):
    out = start_trial(client, db, mails)
    org = org_of(db, "ada@acme-foods.com")
    ws = db.scalar(select(Workspace).where(Workspace.organization_id == org.id))
    org.trial_ends_at = None
    db.commit()
    assert client.get(f"/api/workspaces/{ws.id}", headers=bearer(out["token"])).status_code == 402


def test_paying_customers_and_the_team_are_unaffected_by_the_trial_gate(client, db):
    _, token, ws = make_customer(db)
    assert client.get(f"/api/workspaces/{ws.id}", headers=bearer(token)).status_code == 200
    _, unpaid_token, unpaid_ws = make_customer(db, email="late@example.com", active=False)
    assert client.get(f"/api/workspaces/{unpaid_ws.id}", headers=bearer(unpaid_token)).status_code == 402


def test_a_trial_user_who_pays_becomes_a_normal_customer_with_full_limits(client, db, mails):
    out = start_trial(client, db, mails)
    org = org_of(db, "ada@acme-foods.com")
    billing._activate_plan(db, org.id, "growth", "flutterwave", "cust1", "sub1", paid_until=now_utc() + timedelta(days=365), cycle="annual")
    db.expire_all()
    org = org_of(db, "ada@acme-foods.com")
    assert org.billing_status == "active" and org.plan == "growth" and org.keyword_limit == 999
    assert db.scalar(select(TrialSignup)).status == "converted"
    ws = db.scalar(select(Workspace).where(Workspace.organization_id == org.id))
    assert client.get(f"/api/workspaces/{ws.id}", headers=bearer(out["token"])).status_code == 200
    assert trial.limit(db, org, "keywords", 999) == 999 and client.get("/api/trial/me", headers=bearer(out["token"])).json() == {"is_trial": False}


# ---------------------------------------------------------------- the caps: one workspace, five keywords, and the rest
def test_a_trial_gets_one_workspace_only(client, db, mails):
    start_trial(client, db, mails)
    member = db.scalar(select(OrgMember).where(OrgMember.email == "ada@acme-foods.com"))
    with pytest.raises(auth.AuthError, match="free trial allows up to 1 workspace"):
        auth.add_workspace(db, member, "Second brand", "FMCG")


def test_a_trial_may_track_five_keywords_and_no_more(client, db, mails):
    out = start_trial(client, db, mails)
    ws = ws_of(db)
    h = bearer(out["token"])
    assert client.patch(f"/api/workspaces/{ws.id}", headers=h, json={"keywords": ["a1", "b2", "c3", "d4", "e5"]}).status_code == 200
    r = client.patch(f"/api/workspaces/{ws.id}", headers=h, json={"keywords": ["a1", "b2", "c3", "d4", "e5", "f6"]})
    assert r.status_code == 422 and "free trial allows up to 5 tracked keywords" in r.json()["detail"]


def test_paid_plans_keep_their_own_wording_and_limits(client, db):
    _, token, ws = make_customer(db, plan="standard")
    r = client.patch(f"/api/workspaces/{ws.id}", headers=bearer(token), json={"keywords": [f"k{i}" for i in range(26)]})
    assert r.status_code == 422 and r.json()["detail"] == "Your plan allows up to 25 tracked keywords"


def test_competitors_and_team_members_are_capped(client, db, mails):
    out = start_trial(client, db, mails)
    ws, h = ws_of(db), bearer(out["token"])
    for name in ("Rival One", "Rival Two", "Rival Three"):
        assert client.post(f"/api/workspaces/{ws.id}/competitors", headers=h, json={"name": name}).status_code == 200
    r = client.post(f"/api/workspaces/{ws.id}/competitors", headers=h, json={"name": "Rival Four"})
    assert r.status_code == 422 and "free trial allows tracking up to 3 competitors" in r.json()["detail"]
    for i in (1, 2):
        assert client.post("/api/team/invite", headers=h, json={"email": f"m{i}@acme-foods.com", "name": f"M{i}", "role": "member"}).status_code == 200
    r = client.post("/api/team/invite", headers=h, json={"email": "m3@acme-foods.com", "name": "M3", "role": "member"})
    assert r.status_code in (400, 422) and "free trial allows up to 3 team members" in r.text


def test_history_is_limited_during_a_trial_and_not_for_paying_customers(client, db, mails):
    start_trial(client, db, mails)
    org = org_of(db, "ada@acme-foods.com")
    assert trial.limit(db, org, "historical_days", 3650) == 30
    paid = Organization(name="P", billing_status="active")
    assert trial.limit(db, paid, "historical_days", 3650) == 3650


def test_ai_drafts_are_counted_and_stop_at_the_cap(client, db, mails, monkeypatch):
    open_trials(db, caps={"ai_drafts": 2})
    out = start_trial(client, db, mails)
    ws = ws_of(db)
    case = media_room.open_case(db, add_incident(db, ws, title="Fake page", severity="HIGH", ref="T-1"))
    monkeypatch.setattr(media_room, "draft_statement_ai", lambda *a, **k: "A draft.")
    h = bearer(out["token"])
    for _ in range(2):
        assert client.post("/api/media-room/ai-draft", headers=h, json={"case_id": case.id}).json() == {"draft": "A draft."}
    r = client.post("/api/media-room/ai-draft", headers=h, json={"case_id": case.id})
    assert r.status_code == 422 and "free trial includes 2 AI statement drafts" in r.json()["detail"]
    assert client.get("/api/trial/me", headers=h).json()["usage"]["ai_drafts"] == 2


def test_ask_brandslens_questions_are_counted_and_capped_and_paid_accounts_are_not(client, db, mails, monkeypatch):
    open_trials(db, caps={"ask_messages": 1})
    out = start_trial(client, db, mails)
    ws = ws_of(db)
    monkeypatch.setattr(chatbot, "answer_app_chat", lambda *a, **k: "An answer.")
    h = bearer(out["token"])
    assert client.post(f"/api/app-chat/{ws.id}", headers=h, json={"message": "What is new?"}).status_code == 200
    r = client.post(f"/api/app-chat/{ws.id}", headers=h, json={"message": "And now?"})
    assert r.status_code == 422 and "free trial includes 1 Ask BrandsLens questions" in r.json()["detail"]
    _, ptoken, pws = make_customer(db, plan="professional", email="paid@example.com")
    for _ in range(3):
        assert client.post(f"/api/app-chat/{pws.id}", headers=bearer(ptoken), json={"message": "Hi"}).status_code == 200
    assert (db.get(Organization, db.scalar(select(OrgMember).where(OrgMember.email == "paid@example.com")).organization_id).trial_ask_messages or 0) == 0


# ---------------------------------------------------------------- the team's controls
ADMIN_ROUTES = [("GET", "/api/admin/trials", None), ("GET", "/api/admin/trials/settings", None), ("PUT", "/api/admin/trials/settings", {}),
                ("GET", "/api/admin/trials/export.csv", None), ("POST", "/api/admin/trials/bulk-extend", {"days": 1}), ("GET", "/api/admin/trials/nurture", None),
                ("PUT", "/api/admin/trials/nurture", {"steps": []}), ("POST", "/api/admin/trials/nurture/test", {"step_key": "welcome", "to": "a@b.com"}),
                ("POST", "/api/admin/trials/run-lifecycle", None), ("POST", "/api/admin/trials/x/extend", {"days": 1}), ("POST", "/api/admin/trials/x/set-end", {"ends_at": "2030-01-01T00:00:00Z"}),
                ("POST", "/api/admin/trials/x/end", None), ("POST", "/api/admin/trials/x/block", None), ("POST", "/api/admin/trials/x/unblock", None), ("POST", "/api/admin/trials/x/approve", None)]


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_only_the_brandslens_team_can_use_the_trial_controls(client, db, method, path, body):
    _, token, _ = make_customer(db)
    assert client.request(method, path, headers=bearer(token), json=body).status_code == 403
    assert client.request(method, path, json=body).status_code == 401


def test_settings_round_trip_through_the_api(client, db, admin):
    got = client.get("/api/admin/trials/settings", headers=admin).json()
    assert got["settings"]["enabled"] is False and got["summary"]["total"] == 0 and got["bounds"]["keywords"] == [1, 50]
    r = client.put("/api/admin/trials/settings", headers=admin, json={"enabled": True, "days": 7, "caps": {"keywords": 4}})
    assert r.status_code == 200 and r.json()["settings"]["days"] == 7 and r.json()["settings"]["caps"]["keywords"] == 4
    assert client.put("/api/admin/trials/settings", headers=admin, json={"days": 500}).status_code == 422
    assert client.get("/api/public/trial/status").json() == {"open": True, "reason": "", "days": 7, "keywords": 4, "workspaces": 1}


def lead_id(db, email):
    return db.scalar(select(TrialSignup).where(TrialSignup.email == email)).id


def test_the_lead_list_filters_searches_and_counts(client, db, mails, admin):
    start_trial(client, db, mails)
    start_trial(client, db, mails, email="jo@other-company.com", company="Other Company")
    db.scalar(select(TrialSignup).where(TrialSignup.email == "jo@other-company.com")).status = "expired"
    db.commit()
    everyone = client.get("/api/admin/trials", headers=admin).json()
    assert everyone["total"] == 2 and everyone["summary"]["counts"]["active"] == 1 and everyone["summary"]["counts"]["expired"] == 1
    assert [r["email"] for r in client.get("/api/admin/trials?status=active", headers=admin).json()["rows"]] == ["ada@acme-foods.com"]
    assert [r["company"] for r in client.get("/api/admin/trials?q=OTHER", headers=admin).json()["rows"]] == ["Other Company"]
    row = client.get("/api/admin/trials?q=acme-foods", headers=admin).json()["rows"][0]
    assert row["days_left"] == 30 and row["keywords"] == 5 and row["marketing_consent"] is True and row["billing_status"] == "trialing"


def test_extending_a_running_trial_adds_days_to_its_end(client, db, mails, admin):
    start_trial(client, db, mails)
    before = db.scalar(select(TrialSignup)).ends_at
    r = client.post(f"/api/admin/trials/{lead_id(db, 'ada@acme-foods.com')}/extend", headers=admin, json={"days": 7})
    assert r.status_code == 200 and r.json()["lead"]["days_left"] == 37
    db.expire_all()
    assert db.scalar(select(TrialSignup)).ends_at == before + timedelta(days=7) and org_of(db, "ada@acme-foods.com").trial_ends_at == before + timedelta(days=7)
    assert client.post(f"/api/admin/trials/{lead_id(db, 'ada@acme-foods.com')}/extend", headers=admin, json={"days": 0}).status_code == 422


def test_extending_an_expired_trial_reopens_it_but_never_a_paying_customer(client, db, mails, admin):
    out = start_trial(client, db, mails)
    org, row = org_of(db, "ada@acme-foods.com"), db.scalar(select(TrialSignup))
    ws = ws_of(db)
    org.trial_ends_at = row.ends_at = now_utc() - timedelta(days=2)
    db.commit()
    assert client.get(f"/api/workspaces/{ws.id}", headers=bearer(out["token"])).status_code == 402
    client.post(f"/api/admin/trials/{row.id}/extend", headers=admin, json={"days": 5})
    assert client.get(f"/api/workspaces/{ws.id}", headers=bearer(out["token"])).status_code == 200
    db.expire_all()
    assert db.scalar(select(TrialSignup)).status == "active"
    billing._activate_plan(db, org.id, "growth", "flutterwave", "c", "s", paid_until=now_utc() + timedelta(days=30), cycle="monthly")
    r = client.post(f"/api/admin/trials/{row.id}/end", headers=admin)
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Organization, org.id).billing_status == "active"                      # ending a trial never touches someone who paid


def test_ending_a_trial_now_locks_it_and_setting_a_future_end_reopens_it(client, db, mails, admin):
    out = start_trial(client, db, mails)
    ws, row = ws_of(db), db.scalar(select(TrialSignup))
    client.post(f"/api/admin/trials/{row.id}/end", headers=admin)
    assert client.get(f"/api/workspaces/{ws.id}", headers=bearer(out["token"])).status_code == 402
    future = (now_utc() + timedelta(days=3)).isoformat()
    assert client.post(f"/api/admin/trials/{row.id}/set-end", headers=admin, json={"ends_at": future}).status_code == 200
    assert client.get(f"/api/workspaces/{ws.id}", headers=bearer(out["token"])).status_code == 200


def test_blocking_locks_a_trial_and_stops_a_new_one(client, db, mails, admin):
    out = start_trial(client, db, mails)
    ws, row = ws_of(db), db.scalar(select(TrialSignup))
    client.post(f"/api/admin/trials/{row.id}/block", headers=admin)
    assert client.get(f"/api/workspaces/{ws.id}", headers=bearer(out["token"])).status_code == 402
    assert begin(client).status_code == 403
    assert client.post(f"/api/admin/trials/{row.id}/unblock", headers=admin).json()["lead"]["status"] == "expired"


def test_bulk_extend_only_ever_lengthens_running_trials(client, db, mails, admin):
    start_trial(client, db, mails)
    start_trial(client, db, mails, email="jo@other-company.com", company="Other Company")
    expired = db.scalar(select(TrialSignup).where(TrialSignup.email == "jo@other-company.com"))
    expired.status = "expired"
    db.commit()
    a_before = db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@acme-foods.com")).ends_at
    assert client.post("/api/admin/trials/bulk-extend", headers=admin, json={"days": 10}).json() == {"extended": 1}
    db.expire_all()
    assert db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@acme-foods.com")).ends_at == a_before + timedelta(days=10)
    earlier = (now_utc() + timedelta(days=2)).isoformat()
    assert client.post("/api/admin/trials/bulk-extend", headers=admin, json={"until": earlier}).json() == {"extended": 0}     # an earlier date shortens nothing
    for bad in ({}, {"days": 5, "until": earlier}, {"days": 0}, {"days": 999}):
        assert client.post("/api/admin/trials/bulk-extend", headers=admin, json=bad).status_code == 422


def test_the_lead_export_has_every_lead_the_right_columns_and_no_customer_content(client, db, mails, admin):
    start_trial(client, db, mails, utm_source="producthunt", utm_campaign="official_launch")
    r = client.get("/api/admin/trials/export.csv", headers=admin)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "no-store" in r.headers["cache-control"]
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0] == ",".join(trial.CSV_COLUMNS) and len(lines) == 2
    assert "ada@acme-foods.com" in lines[1] and "producthunt" in lines[1] and ",yes," in lines[1]
    for forbidden in ("password", "token", "code_hash", "incident", "statement"):
        assert forbidden not in lines[0].lower()


def test_the_export_cannot_be_used_to_run_formulas_in_a_spreadsheet(client, db, mails, admin):
    start_trial(client, db, mails, company="=HYPERLINK(\"http://evil.example\",\"x\")")
    db.scalar(select(TrialSignup)).name = "+SUM(1+1)"
    db.commit()
    csv_text = client.get("/api/admin/trials/export.csv", headers=admin).text
    assert "'=HYPERLINK" in csv_text and "'+SUM" in csv_text and ",=HYPERLINK" not in csv_text


# ---------------------------------------------------------------- people without a business email
def test_someone_without_a_business_email_can_ask_for_access_and_the_team_approves_it(client, db, mails, admin):
    open_trials(db)
    r = client.post("/api/public/trial/request-access", json={"email": "ada@gmail.com", "name": "Ada Okafor", "company": "Acme Foods", "note": "We use Gmail for business."})
    assert r.status_code == 200
    assert any(m["to"] == "info@brandslens.com" and "access request" in m["subject"] for m in mails)
    assert begin(client, email="ada@gmail.com").status_code == 422                     # not approved yet
    lead = client.get("/api/admin/trials?status=requested", headers=admin).json()["rows"][0]
    assert lead["note"] == "We use Gmail for business."
    assert client.post(f"/api/admin/trials/{lead['id']}/approve", headers=admin).json()["lead"]["status"] == "approved"
    assert any(m["to"] == "ada@gmail.com" and "approved" in m["subject"] for m in mails)
    assert begin(client, email="ada@gmail.com").status_code == 200
    assert client.post("/api/public/trial/verify", json={"email": "ada@gmail.com", "code": code_for(mails, "ada@gmail.com"), "password": PASSWORD}).status_code == 200


def test_a_business_email_is_told_to_start_directly_and_other_bad_requests_are_refused(client, db):
    open_trials(db)
    assert client.post("/api/public/trial/request-access", json={"email": "ada@acme-foods.com", "name": "Ada", "company": "Acme"}).status_code == 422
    assert client.post("/api/public/trial/request-access", json={"email": "not-an-email", "name": "Ada", "company": "Acme"}).status_code == 422
    assert client.post("/api/public/trial/request-access", json={"email": "a@gmail.com", "name": "", "company": "Acme"}).status_code == 422
    assert trial.approve.__name__ == "approve"
    with pytest.raises(trial.TrialError):
        trial.approve(db, TrialSignup(email="x@y.com", domain="y.com", status="active"))


# ---------------------------------------------------------------- the nurture sequence
def sent_to(mails, email):
    return [m["subject"] for m in mails if m["to"] == email]


def test_the_sequence_has_the_expected_steps_and_the_team_can_edit_them(client, db, admin):
    got = client.get("/api/admin/trials/nurture", headers=admin).json()
    assert [s["key"] for s in got["steps"]] == ["welcome", "day1_setup", "day3_clock", "day7_respond", "day14_prove", "before_end_7", "before_end_3", "before_end_1", "ended", "after_end_3", "after_end_10"]
    assert all(s["enabled"] for s in got["steps"]) and "unsubscribe_url" in got["variables"]
    steps = got["steps"]
    steps[1].update(subject="Edited subject", body="<p>Edited {{first_name}}</p>", days=2, enabled=False)
    out = client.put("/api/admin/trials/nurture", headers=admin, json={"steps": steps}).json()["steps"]
    assert out[1]["subject"] == "Edited subject" and out[1]["days"] == 2 and out[1]["enabled"] is False
    assert client.get("/api/admin/trials/nurture", headers=admin).json()["steps"][1]["subject"] == "Edited subject"
    bad = [dict(steps[0], key="nope"), dict(steps[0], subject=""), dict(steps[0], body=" "), dict(steps[0], days=999), dict(steps[0], enabled="yes"), dict(steps[0], days=True)]
    for item in bad:
        assert client.put("/api/admin/trials/nurture", headers=admin, json={"steps": [item]}).status_code == 422


def test_each_email_goes_out_once_at_its_time_and_never_twice(client, db, mails):
    start_trial(client, db, mails)
    row, t0 = db.scalar(select(TrialSignup)), db.scalar(select(TrialSignup)).started_at
    mails.clear()
    assert trial.run_nurture_for(db, row, t0 + timedelta(hours=12)) == 0
    assert trial.run_nurture_for(db, row, t0 + timedelta(days=1, minutes=1)) == 1
    assert trial.run_nurture_for(db, row, t0 + timedelta(days=1, minutes=5)) == 0
    assert trial.run_nurture_for(db, row, t0 + timedelta(days=3, minutes=1)) == 1
    assert sent_to(mails, "ada@acme-foods.com") == ["It's 2am and someone is talking about your brand", "The first four hours"]


def test_only_the_latest_due_email_is_sent_so_nobody_gets_a_burst(client, db, mails):
    start_trial(client, db, mails)
    row, t0 = db.scalar(select(TrialSignup)), db.scalar(select(TrialSignup)).started_at
    mails.clear()
    assert trial.run_nurture_for(db, row, t0 + timedelta(days=15)) == 1
    assert sent_to(mails, "ada@acme-foods.com") == ["The conversation with the boss"]
    skipped = {e.step_key for e in db.scalars(select(TrialEmail).where(TrialEmail.skipped == True)).all()}         # noqa: E712
    assert {"day1_setup", "day3_clock", "day7_respond"} <= skipped


def test_tips_and_offers_go_only_to_people_who_agreed_but_trial_notices_go_to_everyone(client, db, mails):
    start_trial(client, db, mails, consent=False)
    row, t0 = db.scalar(select(TrialSignup)), db.scalar(select(TrialSignup)).started_at
    mails.clear()
    trial.run_nurture_for(db, row, t0 + timedelta(days=1, minutes=1))                  # a tip: needs consent
    assert sent_to(mails, "ada@acme-foods.com") == []
    trial.run_nurture_for(db, row, t0 + timedelta(days=27, minutes=1))                 # "ends in 3 days": part of the service
    assert sent_to(mails, "ada@acme-foods.com") == ["Three days left on your trial"]
    trial.run_nurture_for(db, row, row.ends_at + timedelta(minutes=1))
    assert "Your trial has ended, but your workspace is safe" in sent_to(mails, "ada@acme-foods.com")


def test_unsubscribing_stops_tips_through_a_working_one_click_link(client, db, mails):
    start_trial(client, db, mails)
    row, t0 = db.scalar(select(TrialSignup)), db.scalar(select(TrialSignup)).started_at
    mails.clear()
    trial.run_nurture_for(db, row, t0 + timedelta(days=1, minutes=1))
    tip = mails[-1]["html"]
    link = re.search(r'href="[^"]*(/api/public/trial/unsubscribe\?token=[^"]+)"', tip).group(1)
    page = client.get(link)
    assert page.status_code == 200 and "unsubscribed" in page.text
    db.expire_all()
    row = db.scalar(select(TrialSignup))
    assert row.unsubscribed_at is not None and row.marketing_consent is False
    mails.clear()
    trial.run_nurture_for(db, row, t0 + timedelta(days=3, minutes=1))
    assert mails == []
    assert "isn't valid" in client.get("/api/public/trial/unsubscribe?token=wrong").text and client.get("/api/public/trial/unsubscribe").status_code == 200


def test_tips_carry_an_unsubscribe_link_and_notices_do_not_and_values_are_escaped(client, db, mails):
    start_trial(client, db, mails, company="<script>alert(1)</script> Ltd")
    row, t0 = db.scalar(select(TrialSignup)), db.scalar(select(TrialSignup)).started_at
    welcome = next(m for m in mails if "trial is live" in m["subject"])
    assert "<script>" not in welcome["html"] and "&lt;script&gt;" in welcome["html"] and "Unsubscribe" not in welcome["html"]
    mails.clear()
    trial.run_nurture_for(db, row, t0 + timedelta(days=1, minutes=1))
    assert "Unsubscribe" in mails[-1]["html"] and "{{" not in mails[-1]["html"]


def test_the_launch_code_appears_in_the_ending_emails_only_when_one_is_set(client, db, mails):
    start_trial(client, db, mails)
    row, t0 = db.scalar(select(TrialSignup)), db.scalar(select(TrialSignup)).started_at
    mails.clear()
    trial.run_nurture_for(db, row, t0 + timedelta(days=23, minutes=1))
    assert "Use code" not in mails[-1]["html"]
    trial.save_settings(db, "t", {"offer_code": "trial15"})
    trial.run_nurture_for(db, row, t0 + timedelta(days=27, minutes=1))
    assert "Your offer code <b>TRIAL15</b>" in mails[-1]["html"]


def test_a_short_trial_skips_the_steps_that_would_not_fit(client, db, mails):
    open_trials(db, days=7)
    start_trial(client, db, mails)
    row, t0 = db.scalar(select(TrialSignup)), db.scalar(select(TrialSignup)).started_at
    keys = {s["key"] for _, s in trial.due_steps(db, row, t0 + timedelta(days=30))}
    assert "before_end_7" not in keys and "day7_respond" not in keys and "day14_prove" not in keys
    assert {"before_end_3", "before_end_1", "ended"} <= keys


def test_the_scheduled_run_ends_due_trials_and_sends_the_ending_email(client, db, mails):
    start_trial(client, db, mails)
    row = db.scalar(select(TrialSignup))
    org = org_of(db, "ada@acme-foods.com")
    org.trial_ends_at = row.ends_at = now_utc() - timedelta(minutes=5)
    db.commit()
    mails.clear()
    out = trial.run_lifecycle(db)
    assert out["expired"] == 1
    db.expire_all()
    assert db.get(Organization, org.id).billing_status == "unpaid" and db.scalar(select(TrialSignup)).status == "expired"
    assert "Your trial has ended, but your workspace is safe" in sent_to(mails, "ada@acme-foods.com")
    assert trial.run_lifecycle(db) == {"expired": 0, "emails": 0}


def test_a_paying_customer_is_no_longer_nurtured_as_a_trial_lead(client, db, mails):
    start_trial(client, db, mails)
    org = org_of(db, "ada@acme-foods.com")
    billing._activate_plan(db, org.id, "growth", "flutterwave", "c", "s", paid_until=now_utc() + timedelta(days=365), cycle="annual")
    mails.clear()
    assert trial.run_nurture(db, now_utc() + timedelta(days=40)) == 0 and mails == []


def test_the_team_can_send_themselves_a_test_of_any_email(client, db, mails, admin):
    r = client.post("/api/admin/trials/nurture/test", headers=admin, json={"step_key": "day3_clock", "to": "me@brandslens.com"})
    assert r.status_code == 200 and r.json() == {"sent": True}
    assert mails[-1]["to"] == "me@brandslens.com" and "first four hours" in mails[-1]["subject"] and "Ada" in mails[-1]["html"]
    assert client.post("/api/admin/trials/nurture/test", headers=admin, json={"step_key": "nope", "to": "me@brandslens.com"}).status_code == 422
    assert client.post("/api/admin/trials/nurture/test", headers=admin, json={"step_key": "welcome", "to": "bad"}).status_code == 422


# ---------------------------------------------------------------- scanning cost
def test_scanning_covers_running_trials_and_paying_customers_but_not_expired_trials(client, db, mails):
    import app.worker as worker
    start_trial(client, db, mails)
    start_trial(client, db, mails, email="jo@other-company.com", company="Other Company")
    _, _, paid_ws = make_customer(db)
    names = {w.name for w in worker._each_workspace(db)}
    assert {"Acme Foods", "Other Company", "Customer Co"} <= names
    org2 = org_of(db, "jo@other-company.com")
    org2.trial_ends_at = now_utc() - timedelta(days=1)
    db.commit()
    names = {w.name for w in worker._each_workspace(db)}
    assert "Other Company" not in names and {"Acme Foods", "Customer Co"} <= names


def test_the_public_pages_do_not_leak_anything_about_other_people(client, db, mails):
    open_trials(db)
    status = client.get("/api/public/trial/status")
    assert set(status.json()) == {"open", "reason", "days", "keywords", "workspaces"} and "max-age" in status.headers["cache-control"]
    start_trial(client, db, mails)
    assert "ada@acme-foods.com" not in client.get("/api/public/trial/status").text


# ---------------------------------------------------------------- abuse limits
def test_the_same_address_cannot_be_asked_for_codes_over_and_over(client, db, mails):
    open_trials(db)
    statuses = [begin(client, email="spam@target-company.com", company="Target Company").status_code for _ in range(7)]
    assert statuses[0] == 200 and 429 in statuses and statuses[-1] == 429


def test_one_network_cannot_flood_the_sign_up(client, db, mails):
    open_trials(db)
    statuses = [begin(client, email=f"p{i}@company{i}.com", company=f"Company {i}").status_code for i in range(24)]
    assert statuses[:20] == [200] * 20 and set(statuses[20:]) == {429}


def test_guessing_codes_from_one_network_is_limited(client, db, mails):
    open_trials(db)
    begin(client)
    statuses = [client.post("/api/public/trial/verify", json={"email": "ada@acme-foods.com", "code": "000000", "password": PASSWORD}).status_code for _ in range(34)]
    assert statuses[-1] == 429 and statuses.count(429) >= 4


def test_access_requests_are_limited_too(client, db, mails):
    open_trials(db)
    codes = [client.post("/api/public/trial/request-access", json={"email": f"x{i}@gmail.com", "name": "Ada", "company": "Acme"}).status_code for i in range(8)]
    assert codes[:5] == [200] * 5 and codes[-1] == 429
