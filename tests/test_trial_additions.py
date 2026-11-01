"""Gaps closed after auditing the trial: one more free provider, Mailchimp for consenting leads only (and in step on unsubscribe),
and the chatbot and Terms no longer contradict the trial."""
import re

import pytest

from app import legal_content
from app.models import TrialSignup
from app.services import chatbot, mailer, trial
from sqlalchemy import select

PASSWORD = "correct-horse-1"


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_email", lambda to, subject, html_body, from_addr="", reply_to="": sent.append({"to": to, "subject": subject, "html": html_body}) or True)
    return sent


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    from app import routes_trial
    for limiter in (routes_trial._start_ip, routes_trial._start_email, routes_trial._verify_ip, routes_trial._request_ip):
        limiter._hits.clear()
    yield


@pytest.fixture
def chimp(monkeypatch):
    calls = {"add": [], "unsub": []}
    monkeypatch.setattr(mailer, "add_to_mailchimp", lambda email, name="": calls["add"].append((email, name)) or True)
    monkeypatch.setattr(mailer, "unsubscribe_from_mailchimp", lambda email: calls["unsub"].append(email) or True)
    return calls


def go(client, db, mails, email="ada@acme-foods.com", consent=True):
    trial.save_settings(db, "test", {"enabled": True})
    assert client.post("/api/public/trial/start", json={"email": email, "name": "Ada Okafor", "company": "Acme Foods", "marketing_consent": consent}).status_code == 200
    mail = next(m for m in reversed(mails) if m["to"] == email and "verification code" in m["subject"])
    code = re.search(r">(\d{6})<", mail["html"]).group(1)
    r = client.post("/api/public/trial/verify", json={"email": email, "code": code, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


def text_of(module):
    return " ".join(v for v in vars(module).values() if isinstance(v, str))


# ---------------------------------------------------------------- who may start a trial
@pytest.mark.parametrize("domain", ["zoho.com", "zoho.eu", "zohomail.com", "zohomail.in"])
def test_free_zoho_addresses_cannot_start_a_trial(domain):
    assert trial.is_business_email(f"a@{domain}")[0] is False


def test_a_company_using_zoho_with_its_own_domain_still_can():
    assert trial.is_business_email("ada@acme-foods.com")[0] is True and trial.is_business_email("ada@zoho-partners.co.ng")[0] is True


# ---------------------------------------------------------------- Mailchimp
def test_a_lead_who_agreed_to_marketing_emails_joins_mailchimp_with_their_first_name(client, db, mails, chimp):
    go(client, db, mails, consent=True)
    assert chimp["add"] == [("ada@acme-foods.com", "Ada")]


def test_a_lead_who_did_not_agree_is_never_sent_to_mailchimp(client, db, mails, chimp):
    go(client, db, mails, consent=False)
    assert chimp["add"] == []


def test_mailchimp_being_down_does_not_break_the_sign_up(client, db, mails, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Mailchimp is down")
    monkeypatch.setattr(mailer, "add_to_mailchimp", boom)
    body = go(client, db, mails, consent=True)
    assert body["token"] and body["trial_ends_at"]


def test_unsubscribing_through_the_email_link_also_unsubscribes_in_mailchimp(client, db, mails, chimp):
    go(client, db, mails, consent=True)
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@acme-foods.com"))
    assert client.get(f"/api/public/trial/unsubscribe?token={row.unsub_token}").status_code == 200
    assert chimp["unsub"] == ["ada@acme-foods.com"]
    db.refresh(row)
    assert row.marketing_consent is False and row.unsubscribed_at is not None


def test_a_bad_unsubscribe_link_changes_nothing_in_mailchimp(client, db, mails, chimp):
    go(client, db, mails, consent=True)
    client.get("/api/public/trial/unsubscribe?token=not-a-real-token")
    assert chimp["unsub"] == []


def test_mailchimp_unsubscribe_does_nothing_when_mailchimp_is_not_set_up(monkeypatch):
    monkeypatch.setattr(mailer, "MAILCHIMP_API_KEY", "")
    monkeypatch.setattr(mailer.httpx, "patch", lambda *a, **k: pytest.fail("no request should be made"))
    assert mailer.unsubscribe_from_mailchimp("a@b.com") is False


def test_mailchimp_unsubscribe_sends_the_right_request(monkeypatch):
    for name, value in (("MAILCHIMP_API_KEY", "key-us1"), ("MAILCHIMP_SERVER_PREFIX", "us1"), ("MAILCHIMP_LIST_ID", "list9")):
        monkeypatch.setattr(mailer, name, value)
    seen = {}

    class Ok:
        status_code = 200
        def raise_for_status(self): pass

    def fake(url, **kw):
        seen.update(url=url, **kw)
        return Ok()
    monkeypatch.setattr(mailer.httpx, "patch", fake)
    assert mailer.unsubscribe_from_mailchimp("Ada@Acme-Foods.com") is True
    import hashlib
    assert seen["url"].endswith("/lists/list9/members/" + hashlib.md5(b"ada@acme-foods.com").hexdigest())     # same lower-cased hash the subscribe call uses
    assert seen["json"] == {"status": "unsubscribed"} and seen["auth"] == ("anystring", "key-us1")


def test_someone_who_was_never_on_the_mailchimp_list_is_not_an_error(monkeypatch):
    for name, value in (("MAILCHIMP_API_KEY", "k"), ("MAILCHIMP_SERVER_PREFIX", "us1"), ("MAILCHIMP_LIST_ID", "l")):
        monkeypatch.setattr(mailer, name, value)

    class Missing:
        status_code = 404
        def raise_for_status(self): raise AssertionError("a 404 must not be treated as a failure")
    monkeypatch.setattr(mailer.httpx, "patch", lambda *a, **k: Missing())
    assert mailer.unsubscribe_from_mailchimp("x@y.com") is False


def test_a_mailchimp_failure_on_unsubscribe_is_swallowed(monkeypatch):
    for name, value in (("MAILCHIMP_API_KEY", "k"), ("MAILCHIMP_SERVER_PREFIX", "us1"), ("MAILCHIMP_LIST_ID", "l")):
        monkeypatch.setattr(mailer, name, value)
    def boom(*a, **k): raise RuntimeError("network down")
    monkeypatch.setattr(mailer.httpx, "patch", boom)
    assert mailer.unsubscribe_from_mailchimp("x@y.com") is False


# ---------------------------------------------------------------- wording that must not contradict the trial
def test_the_chatbot_no_longer_tells_visitors_there_is_no_trial():
    t = text_of(chatbot)
    assert "There is NO free trial" not in t
    assert "www.brandslens.com/pricing" in t and "Never state a length" in t


def test_the_terms_describe_the_trial_that_really_exists():
    t = text_of(legal_content)
    assert "convert to a paid subscription unless" not in t
    assert "needs no payment card and never turns into a paid plan" in t and "unsubscribe at any time" in t
