"""The standing first-purchase offer: one ordinary code that applies itself to every customer's first purchase, independent of any launch.
The server decides who qualifies and what is charged. Flutterwave is replaced by a stand-in that records what it was asked."""
import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Organization, PromoCode, SignupAttribution, TrialSignup, now_utc
from app.services import billing, promos, trial
from tests.helpers import bearer, make_admin, make_customer, seed_valid_codes


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
    return db.scalar(select(Organization).where(Organization.name == "Customer Co")), bearer(token)


def body(**over):
    d = dict(name="Ada Obi", email="ada@example.com", password="longenough1", company="Ada Co", sector="FMCG", plan="growth", cycle="annual", quantity=1, pay_in_ngn=False)
    d.update(over)
    return d


def price(db, plan="growth", cycle="annual"):
    return billing.get_plan_catalog(db)[plan][billing.CYCLE_KEYS[cycle][0]]


def webhook(db, call, tx_id=777):
    payload = {"event": "charge.completed", "data": {"id": tx_id, "tx_ref": call["tx_ref"], "status": "successful", "amount": call["amount"], "currency": call.get("currency", "USD"),
                                                   "meta": call["meta"], "customer": {"id": 5}}}
    return billing.handle_flutterwave_webhook(db, json.dumps(payload).encode(), "test-hash")


def turn_on(db, code="FIRST15", percent=15, **kw):
    kw.setdefault("active", True)
    db.add(PromoCode(code=code, percent=percent, platform="Standing offer", **kw))
    db.commit()
    promos.set_standing_code(db, code)


def new_customer(client, **over):
    return client.post("/api/billing/pending-checkout", json=body(**over))


def pay(client, headers, **over):
    return client.post("/api/billing/checkout", headers=headers, json={"plan": "growth", "cycle": "annual", "provider": "flutterwave", **over})


FIRST_YEAR = lambda db, pct=15: round(price(db) - price(db) * pct / 100, 2)


# ================================================================ a NEW customer (the pay-first sign-up)
def test_switched_off_nothing_changes(client, db, fw):
    assert new_customer(client).status_code == 200
    call = fw[0]
    assert call["amount"] == price(db) and call["payment_plan"] is not None and "promo_percent" not in call["meta"]
    row = db.scalar(select(SignupAttribution))
    assert (row.promo_code, row.discount_percent, row.discount_amount) == ("", 0, 0)          # still tracked for the report, but no offer


def test_a_new_customer_gets_15_percent_off_the_first_year_with_no_code(client, db, fw):
    turn_on(db)
    assert new_customer(client).status_code == 200
    call = fw[0]
    assert call["amount"] == FIRST_YEAR(db) == round(price(db) * 0.85, 2)
    assert call["payment_plan"] is None and call["meta"]["promo_percent"] == 15          # a one-time prepaid year, renewing at full price afterwards
    row = db.scalar(select(SignupAttribution))
    assert (row.status, row.promo_code, row.discount_percent, row.discount_amount, row.charged_amount) == ("started", "FIRST15", 15, round(price(db) * 0.15, 2), call["amount"])


def test_the_discount_is_counted_as_redeemed_only_once_the_payment_is_proved(client, db, fw):
    turn_on(db)
    new_customer(client)
    promo = db.scalar(select(PromoCode).where(PromoCode.code == "FIRST15"))
    assert promos.redemptions(db, promo) == 0                       # an abandoned checkout uses nothing up
    webhook(db, fw[0])
    assert promos.redemptions(db, promo) == 1 and db.scalar(select(SignupAttribution)).status == "paid"
    assert db.scalar(select(Organization).where(Organization.name == "Ada Co")) is not None


@pytest.mark.parametrize("cycle", ["monthly", "daily"])
def test_monthly_and_daily_plans_are_not_discounted(client, db, fw, cycle):
    turn_on(db)
    assert new_customer(client, cycle=cycle).status_code == 200
    call = fw[0]
    assert call["amount"] == price(db, cycle=cycle) and "promo_percent" not in call["meta"]
    row = db.scalar(select(SignupAttribution))
    assert (row.promo_code, row.discount_percent) == ("", 0)


def test_when_several_years_are_bought_only_the_first_is_discounted(client, db, fw):
    turn_on(db)
    assert new_customer(client, quantity=3).status_code == 200
    assert fw[0]["amount"] == round(price(db) * 3 - price(db) * 0.15, 2)


def test_it_works_when_paying_in_naira(client, db, fw):
    turn_on(db)
    assert new_customer(client, pay_in_ngn=True).status_code == 200
    call = fw[0]
    assert call["currency"] == "NGN" and call["amount"] == billing.signup_pricing(db, "growth", "annual", 1, 15, True)["amount"] < billing.signup_pricing(db, "growth", "annual", 1, 0, True)["amount"]


# ---------------------------------------------------------------- the standing offer and a typed code are never added together
def test_a_typed_code_of_the_same_size_wins_so_its_platform_is_still_counted(client, db, fw):
    turn_on(db); seed_valid_codes(db)
    assert new_customer(client, promo_code="ph15").status_code == 200
    assert fw[0]["amount"] == FIRST_YEAR(db) and db.scalar(select(SignupAttribution)).promo_code == "PH15"


def test_a_smaller_typed_code_loses_to_the_standing_offer(client, db, fw):
    turn_on(db); db.add(PromoCode(code="SMALL10", percent=10, active=True)); db.commit()
    assert new_customer(client, promo_code="SMALL10").status_code == 200
    assert fw[0]["amount"] == FIRST_YEAR(db, 15) and db.scalar(select(SignupAttribution)).promo_code == "FIRST15"


def test_a_bigger_typed_code_beats_the_standing_offer_and_nothing_is_stacked(client, db, fw):
    turn_on(db); db.add(PromoCode(code="BIG25", percent=25, active=True)); db.commit()
    assert new_customer(client, promo_code="BIG25").status_code == 200
    assert fw[0]["amount"] == FIRST_YEAR(db, 25) and fw[0]["amount"] > price(db) * (1 - 0.15 - 0.25)


def test_a_wrong_typed_code_is_still_refused_not_quietly_replaced(client, db, fw):
    turn_on(db)
    r = new_customer(client, promo_code="NOPE99")
    assert r.status_code == 422 and "isn't valid" in r.json()["detail"] and fw == []


# ---------------------------------------------------------------- the offer's own limits
def test_a_standing_offer_with_a_cap_stops_when_it_is_used_up(client, db, fw):
    turn_on(db, max_redemptions=1)
    new_customer(client); webhook(db, fw[0])
    assert new_customer(client, email="bola@example.com", company="Bola Co").status_code == 200
    assert fw[1]["amount"] == price(db) and "promo_percent" not in fw[1]["meta"]


def test_an_ended_or_not_yet_started_or_switched_off_code_is_not_applied(client, db, fw):
    turn_on(db, valid_until=now_utc() - timedelta(days=1)); new_customer(client)
    assert fw[0]["amount"] == price(db)
    db.query(PromoCode).delete(); db.commit()
    turn_on(db, code="LATER15", valid_from=now_utc() + timedelta(days=3)); new_customer(client, email="b@example.com", company="B Co")
    assert fw[1]["amount"] == price(db)
    db.query(PromoCode).delete(); db.commit()
    turn_on(db, code="OFF15", active=False); new_customer(client, email="c@example.com", company="C Co")
    assert fw[2]["amount"] == price(db)


# ================================================================ a SIGNED-IN account paying from the Billing page
def test_an_account_that_never_paid_gets_it_with_no_code(client, db, fw, acct):
    turn_on(db)
    org, h = acct
    assert pay(client, h).status_code == 200
    call = fw[0]
    assert call["amount"] == FIRST_YEAR(db) and call["payment_plan"] is None and call["meta"]["organization_id"] == org.id and call["meta"]["promo_percent"] == 15
    row = db.scalar(select(SignupAttribution))
    assert (row.promo_code, row.organization_id, row.status) == ("FIRST15", org.id, "started")


def test_a_free_trial_account_is_a_first_purchase(client, db, fw, acct):
    turn_on(db)
    org, h = acct
    org.billing_status, org.trial_started_at, org.trial_ends_at = "trialing", now_utc(), now_utc() + timedelta(days=30)
    db.commit()
    assert pay(client, h).status_code == 200 and fw[0]["amount"] == FIRST_YEAR(db)


def test_paying_marks_the_discount_as_redeemed(client, db, fw, acct):
    turn_on(db)
    org, h = acct
    pay(client, h); webhook(db, fw[0])
    assert db.scalar(select(SignupAttribution)).status == "paid" and promos.redemptions(db, db.scalar(select(PromoCode).where(PromoCode.code == "FIRST15"))) == 1


@pytest.mark.parametrize("make", ["active", "past_due", "cancelled", "has_period", "has_provider", "has_subscription", "activated_before"])
def test_someone_who_has_paid_before_does_not_get_it_again(client, db, fw, acct, make):
    turn_on(db)
    org, h = acct
    if make in ("active", "past_due", "cancelled"):
        org.billing_status = make
    elif make == "has_period":
        org.paid_until = now_utc() - timedelta(days=30)
    elif make == "has_provider":
        org.billing_provider = "flutterwave"
    elif make == "has_subscription":
        org.billing_subscription_id = "sub_1"
    else:
        org.plan_activated_at = now_utc() - timedelta(days=400)
    db.commit()
    assert pay(client, h).status_code == 200 and fw[0]["amount"] == price(db) and "promo_percent" not in fw[0]["meta"]


def test_an_earlier_paid_redemption_also_counts_as_having_paid(db, acct):
    org, _ = acct
    db.add(SignupAttribution(pending_signup_id="p1", email="x@y.com", plan="growth", cycle="annual", promo_code="PH15", organization_id=org.id, status="paid")); db.commit()
    assert promos.has_paid_before(db, org) is True


def test_a_team_account_is_never_offered_it(db, acct):
    org, _ = acct
    org.billing_status = "exempt"; db.commit()
    turn_on(db)
    assert promos.standing_for_org(db, org) is None


def test_card_checkout_only_like_every_code(client, db, fw, acct):
    turn_on(db)
    org, h = acct
    r = client.post("/api/billing/checkout", headers=h, json={"plan": "growth", "cycle": "annual", "provider": "stripe"})
    assert fw == [] and r.status_code in (409, 422)           # not configured here; and in any case no standing discount path for it


def test_a_typed_code_with_a_provider_that_cannot_take_codes_is_still_refused(client, db, fw, acct):
    turn_on(db)
    _, h = acct
    r = client.post("/api/billing/checkout", headers=h, json={"plan": "growth", "cycle": "annual", "provider": "paystack", "promo_code": "PH15"})
    assert r.status_code == 422 and "card checkout only" in r.json()["detail"]


# ================================================================ what the website and the screens may ask
def test_the_public_answer_is_off_by_default_and_on_when_switched_on(client, db):
    assert client.get("/api/public/standing-offer").json() == {"active": False}
    turn_on(db)
    got = client.get("/api/public/standing-offer").json()
    assert got == {"active": True, "code": "FIRST15", "percent": 15, "message": "15% off your first year, applied automatically."}


def test_the_public_answer_goes_back_to_off_when_the_code_ends_or_is_used_up(client, db, fw):
    turn_on(db, max_redemptions=1)
    assert client.get("/api/public/standing-offer").json()["active"] is True
    new_customer(client); webhook(db, fw[0])
    assert client.get("/api/public/standing-offer").json() == {"active": False}


def test_the_billing_status_tells_an_unpaid_account_about_it_and_nobody_else(client, db, acct):
    org, h = acct
    assert client.get("/api/billing/status", headers=h).json()["standing_offer"] is None            # switched off
    turn_on(db)
    assert client.get("/api/billing/status", headers=h).json()["standing_offer"]["percent"] == 15
    org.billing_status = "active"; db.commit()
    assert client.get("/api/billing/status", headers=h).json()["standing_offer"] is None            # already a customer


def test_the_preview_the_screens_use_prices_the_standing_code_exactly_as_checkout_charges(client, db, fw):
    turn_on(db)
    q = client.get("/api/public/promo/FIRST15?plan=growth&cycle=annual&quantity=1").json()
    new_customer(client)
    assert q["valid"] is True and q["total"] == fw[0]["amount"] == FIRST_YEAR(db) and q["full_total"] == price(db)


# ================================================================ the switch in Super Admin
@pytest.fixture
def admin(db):
    _, token = make_admin(db)
    return bearer(token)


def test_the_admin_sees_the_standing_offer_off_by_default(client, db, admin):
    assert client.get("/api/admin/standing-offer", headers=admin).json() == {"enabled": False, "code": "FIRST15", "percent": 15, "live": False, "redeemed": 0}


def test_switching_it_on_creates_the_code_with_no_end_date_and_no_limit(client, db, admin):
    r = client.put("/api/admin/standing-offer", headers=admin, json={"enabled": True, "code": " first15 ", "percent": 15})
    assert r.status_code == 200 and r.json() == {"enabled": True, "code": "FIRST15", "percent": 15, "live": True, "redeemed": 0}
    p = db.scalar(select(PromoCode).where(PromoCode.code == "FIRST15"))
    assert (p.percent, p.active, p.valid_from, p.valid_until, p.max_redemptions, p.platform) == (15, True, None, None, None, "Standing offer")
    assert client.get("/api/public/standing-offer").json()["active"] is True


def test_switching_it_on_with_an_existing_code_reuses_it_and_keeps_its_dates(client, db, admin):
    seed_valid_codes(db)
    before = db.scalar(select(PromoCode).where(PromoCode.code == "PH15")).valid_until
    r = client.put("/api/admin/standing-offer", headers=admin, json={"enabled": True, "code": "PH15", "percent": 20})
    assert r.json()["percent"] == 20 and db.scalar(select(PromoCode).where(PromoCode.code == "PH15")).valid_until == before


def test_the_admin_can_change_the_percentage_and_new_customers_get_the_new_one(client, db, admin, fw):
    client.put("/api/admin/standing-offer", headers=admin, json={"enabled": True, "code": "FIRST15", "percent": 15})
    client.put("/api/admin/standing-offer", headers=admin, json={"enabled": True, "code": "FIRST15", "percent": 20})
    new_customer(client)
    assert fw[0]["amount"] == FIRST_YEAR(db, 20)


def test_switching_it_off_stops_it_at_once_and_keeps_the_code(client, db, admin, fw):
    client.put("/api/admin/standing-offer", headers=admin, json={"enabled": True, "code": "FIRST15", "percent": 15})
    r = client.put("/api/admin/standing-offer", headers=admin, json={"enabled": False})
    assert r.json()["enabled"] is False and client.get("/api/public/standing-offer").json() == {"active": False}
    new_customer(client)
    assert fw[0]["amount"] == price(db) and db.scalar(select(PromoCode).where(PromoCode.code == "FIRST15")) is not None


@pytest.mark.parametrize("payload", [{"enabled": True, "code": "AB", "percent": 15}, {"enabled": True, "code": "FIRST15", "percent": 0}, {"enabled": True, "code": "FIRST15", "percent": 91},
                                     {"enabled": True, "code": "!!", "percent": 15}, {"enabled": True, "code": "A" * 30, "percent": 15}])
def test_a_bad_code_or_percentage_is_refused(client, db, admin, payload):
    assert client.put("/api/admin/standing-offer", headers=admin, json=payload).status_code == 422
    assert client.get("/api/public/standing-offer").json() == {"active": False}


def test_only_the_team_can_change_it(client, db):
    _, token, _ = make_customer(db, email="paid@example.com", active=True)           # an ordinary paying customer, not a team account
    h = bearer(token)
    assert client.get("/api/admin/standing-offer", headers=h).status_code == 403
    assert client.put("/api/admin/standing-offer", headers=h, json={"enabled": True, "code": "FIRST15", "percent": 15}).status_code == 403
    assert client.put("/api/admin/standing-offer", json={"enabled": True}).status_code in (401, 403)


def test_the_admin_sees_how_many_customers_used_it(client, db, admin, fw):
    client.put("/api/admin/standing-offer", headers=admin, json={"enabled": True, "code": "FIRST15", "percent": 15})
    new_customer(client); webhook(db, fw[0])
    assert client.get("/api/admin/standing-offer", headers=admin).json()["redeemed"] == 1


# ================================================================ the trial emails
def lead(db):
    from app.models import TrialSignup
    row = TrialSignup(email="ada@acme-foods.com", domain="acme-foods.com", name="Ada Okafor", company="Acme Foods", status="active", marketing_consent=True, started_at=now_utc(), ends_at=now_utc() + timedelta(days=10))
    db.add(row); db.commit()
    return row


def step(db, key):
    return next(s for s in trial.steps(db) if s["key"] == key)


def test_the_ending_email_says_the_discount_is_automatic_when_the_standing_offer_is_on(db):
    turn_on(db)
    _, html_ = trial.build_email(db, lead(db), step(db, "before_end_3"))
    assert "15% off your first year" in html_ and "no code to type" in html_.lower() and "?promo=" not in html_


def test_an_explicit_trial_code_still_takes_priority_in_the_email(db):
    turn_on(db)
    trial.save_settings(db, "t", {"offer_code": "TRIAL15"})
    _, html_ = trial.build_email(db, lead(db), step(db, "before_end_3"))
    assert "TRIAL15" in html_ and "/billing?promo=TRIAL15" in html_


def test_with_no_offer_at_all_the_email_promises_nothing(db):
    _, html_ = trial.build_email(db, lead(db), step(db, "before_end_3"))
    assert "% off" not in html_


def test_someone_who_did_not_agree_to_marketing_gets_no_offer_in_a_service_email(db):
    turn_on(db)
    row = lead(db); row.marketing_consent = False; db.commit()
    _, html_ = trial.build_email(db, row, step(db, "before_end_3"))
    assert "% off" not in html_
