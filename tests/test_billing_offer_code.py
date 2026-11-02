"""A signed-in account (for example a free-trial user) paying with an offer code from the Billing page, with Flutterwave replaced by a
stand-in that records what it was asked. Without a code nothing changes."""
import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Organization, PromoCode, SignupAttribution, TrialSignup, now_utc
from app.services import billing, mailer, promos, trial
from tests.helpers import bearer, make_customer, seed_valid_codes


@pytest.fixture
def fw(monkeypatch):
    calls = []
    monkeypatch.setattr(billing, "_flutterwave_initiate_payment", lambda **kw: calls.append(kw) or "https://pay.test/checkout")
    monkeypatch.setattr(billing, "_verify_flutterwave_transaction", lambda *a, **k: True)
    return calls


@pytest.fixture
def acct(client, db):
    """An account that has not paid yet, signed in as its owner."""
    _, token, _ = make_customer(db, plan="standard", active=False)
    org = db.scalar(select(Organization).where(Organization.name == "Customer Co"))
    return org, bearer(token)


def pay(client, headers, **over):
    return client.post("/api/billing/checkout", headers=headers, json={"plan": "growth", "cycle": "annual", "provider": "flutterwave", **over})


def price(db, plan="growth"):
    return billing.get_plan_catalog(db)[plan][billing.CYCLE_KEYS["annual"][0]]


def webhook(db, call, amount=None, tx_id=901):
    payload = {"event": "charge.completed", "data": {"id": tx_id, "tx_ref": call["tx_ref"], "status": "successful", "amount": call["amount"] if amount is None else amount,
                                                      "currency": call.get("currency", "USD"), "meta": call["meta"], "customer": {"id": 5}}}
    return billing.handle_flutterwave_webhook(db, json.dumps(payload).encode(), "test-hash")


# ---------------------------------------------------------------- without a code nothing changes
def test_without_a_code_the_original_recurring_charge_is_unchanged(client, db, fw, acct):
    org, h = acct
    assert pay(client, h).status_code == 200
    call = fw[0]
    assert call["amount"] == price(db) and call["payment_plan"] == billing.get_plan_catalog(db)["growth"][billing.CYCLE_KEYS["annual"][3]]
    assert "promo_percent" not in call["meta"] and call["meta"]["organization_id"] == org.id
    assert db.scalars(select(SignupAttribution)).all() == []


# ---------------------------------------------------------------- with a code
def test_a_code_takes_15_percent_off_and_is_charged_as_a_one_time_year(client, db, fw, acct):
    org, h = acct
    seed_valid_codes(db)
    assert pay(client, h, promo_code=" ph15 ").json()["checkout_url"] == "https://pay.test/checkout"
    call = fw[0]
    assert call["amount"] == round(price(db) - price(db) * 0.15, 2)
    assert call["payment_plan"] is None and call["meta"]["promo_percent"] == 15 and call["meta"]["organization_id"] == org.id
    row = db.scalar(select(SignupAttribution))
    assert (row.status, row.promo_code, row.discount_percent, row.organization_id) == ("started", "PH15", 15, org.id)
    assert row.charged_amount == call["amount"] and row.discount_amount == round(price(db) * 0.15, 2)


def test_a_code_works_when_paying_in_naira(client, db, fw, acct):
    _, h = acct
    seed_valid_codes(db)
    assert pay(client, h, promo_code="HN15", pay_in_ngn=True).status_code == 200
    call, rate, base = fw[0], billing.get_ngn_rate(db), price(db)
    assert call["currency"] == "NGN" and call["amount"] == round((base - round(base * 0.15, 2)) * rate, 2) and call["payment_plan"] is None


def test_the_discount_is_on_one_year_only_however_many_are_bought(client, db, fw, acct):
    _, h = acct
    seed_valid_codes(db)
    assert pay(client, h, promo_code="PH15", quantity=3).status_code == 200
    assert fw[0]["amount"] == round(price(db) * 3 - price(db) * 0.15, 2)


@pytest.mark.parametrize("code,over,reason", [("NOPE15", {}, "isn't valid"), ("PH15", {"cycle": "monthly"}, "annual plans only"), ("", {"promo_code": "   "}, None)])
def test_a_code_that_cannot_be_used_is_refused_with_the_reason_before_any_payment_link_exists(client, db, fw, acct, code, over, reason):
    _, h = acct
    seed_valid_codes(db)
    r = pay(client, h, **({"promo_code": code} | over))
    if reason:
        assert r.status_code == 422 and reason in r.json()["detail"] and fw == []
        assert db.scalars(select(SignupAttribution)).all() == []
    else:
        assert r.status_code == 200 and "promo_percent" not in fw[0]["meta"]            # a blank code is simply no code


def test_an_offer_that_has_not_started_or_has_ended_says_so(client, db, fw, acct):
    _, h = acct
    db.add(PromoCode(code="LATE15", percent=15, active=True, valid_from=now_utc() + timedelta(days=3), valid_until=now_utc() + timedelta(days=9)))
    db.add(PromoCode(code="OLD15", percent=15, active=True, valid_from=now_utc() - timedelta(days=9), valid_until=now_utc() - timedelta(days=1)))
    db.commit()
    assert "hasn't started yet" in pay(client, h, promo_code="LATE15").json()["detail"]
    assert "has ended" in pay(client, h, promo_code="OLD15").json()["detail"] and fw == []


def test_a_code_cannot_be_used_with_a_provider_that_does_not_support_it(client, db, fw, acct, monkeypatch):
    _, h = acct
    seed_valid_codes(db)
    monkeypatch.setattr(billing, "create_stripe_checkout", lambda *a, **k: pytest.fail("no payment link should be made"))
    r = pay(client, h, provider="stripe", promo_code="PH15")
    assert r.status_code == 422 and "card checkout only" in r.json()["detail"]


# ---------------------------------------------------------------- paying
def test_paying_with_the_code_activates_the_plan_and_counts_the_redemption(client, db, fw, acct):
    org, h = acct
    seed_valid_codes(db)
    pay(client, h, promo_code="PH15")
    webhook(db, fw[0])
    db.refresh(org)
    row = db.scalar(select(SignupAttribution))
    assert org.billing_status == "active" and org.plan == "growth"
    assert row.status == "paid" and row.paid_at and row.charged_amount == fw[0]["amount"]
    assert promos.redemptions(db, db.scalar(select(PromoCode).where(PromoCode.code == "PH15"))) == 1


def test_paying_full_price_some_other_way_never_counts_as_using_the_code(client, db, fw, acct):
    org, h = acct
    seed_valid_codes(db)
    pay(client, h, promo_code="PH15")
    webhook(db, fw[0], amount=price(db))                     # they actually paid the full price
    db.refresh(org)
    assert org.billing_status == "active"                    # they are still activated, of course
    assert db.scalar(select(SignupAttribution)).status == "started" and promos.redemptions(db, db.scalar(select(PromoCode).where(PromoCode.code == "PH15"))) == 0


def test_a_retried_confirmation_never_double_counts(client, db, fw, acct):
    _, h = acct
    seed_valid_codes(db)
    pay(client, h, promo_code="PH15")
    webhook(db, fw[0]); webhook(db, fw[0])
    assert len(db.scalars(select(SignupAttribution).where(SignupAttribution.status == "paid")).all()) == 1


def test_a_limited_offer_closes_after_its_last_paid_purchase(client, db, fw, acct):
    _, h = acct
    db.add(PromoCode(code="ONE15", percent=15, active=True, max_redemptions=1))
    db.commit()
    pay(client, h, promo_code="ONE15")
    webhook(db, fw[0])
    again = pay(client, h, promo_code="ONE15")
    assert again.status_code == 422 and "fully redeemed" in again.json()["detail"]


def test_the_discount_is_checked_by_the_server_not_the_screen(client, db, fw, acct):
    """Nothing the browser sends can lower the price: the amount comes from the catalogue and the code's own percentage."""
    _, h = acct
    seed_valid_codes(db)
    pay(client, h, promo_code="PH15", percent=99, amount=1, discount=900)
    assert fw[0]["amount"] == round(price(db) - price(db) * 0.15, 2) and fw[0]["meta"]["promo_percent"] == 15


# ---------------------------------------------------------------- the free-trial user, end to end
def test_a_free_trial_user_buys_with_the_code_and_becomes_a_converted_customer(client, db, monkeypatch, fw):
    codes = {}
    monkeypatch.setattr(trial, "send_code_email", lambda row, code: codes.__setitem__(row.email, code) or True)
    monkeypatch.setattr(mailer, "send_email", lambda *a, **k: True)
    seed_valid_codes(db)
    trial.save_settings(db, "t", {"enabled": True})
    assert client.post("/api/public/trial/start", json={"email": "ada@acme-foods.com", "name": "Ada Okafor", "company": "Acme Foods", "marketing_consent": True,
                                                        "utm_source": "producthunt", "utm_medium": "launch", "utm_campaign": "official_launch", "utm_term": "ai", "utm_content": "banner_a"}).status_code == 200
    tok = client.post("/api/public/trial/verify", json={"email": "ada@acme-foods.com", "code": codes["ada@acme-foods.com"], "password": "correct-horse-1"}).json()["token"]
    org = db.scalar(select(Organization).where(Organization.name == "Acme Foods"))
    assert org.billing_status == "trialing"
    h = bearer(tok)
    r = pay(client, h, promo_code="PH15")
    assert r.status_code == 200
    row = db.scalar(select(SignupAttribution))
    assert (row.utm_source, row.utm_campaign, row.utm_term, row.utm_content) == ("producthunt", "official_launch", "ai", "banner_a")   # the lead's campaign travels with the sale
    webhook(db, fw[-1])
    db.refresh(org)
    lead = db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@acme-foods.com"))
    assert org.billing_status == "active" and lead.status == "converted"
    assert db.scalar(select(SignupAttribution)).status == "paid"
