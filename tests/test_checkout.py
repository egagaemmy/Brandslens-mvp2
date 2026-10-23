"""The pay-first signup with a launch code, end to end, with Flutterwave replaced by a stand-in that records what it was asked."""
import json

import pytest
from sqlalchemy import select

from app.models import Organization, PendingSignup, SignupAttribution
from app.services import billing, promos
from tests.helpers import seed_valid_codes


@pytest.fixture
def fw(monkeypatch):
    calls = []

    def fake_initiate(**kw):
        calls.append(kw)
        return "https://pay.test/checkout"

    monkeypatch.setattr(billing, "_flutterwave_initiate_payment", fake_initiate)
    monkeypatch.setattr(billing, "_verify_flutterwave_transaction", lambda *a, **k: True)
    return calls


def body(**over):
    d = dict(name="Ada Obi", email="ada@example.com", password="longenough1", company="Ada Co", sector="FMCG",
             plan="growth", cycle="annual", quantity=1, pay_in_ngn=False)
    d.update(over)
    return d


def catalog_price(db, plan="growth", cycle="annual"):
    return billing.get_plan_catalog(db)[plan][billing.CYCLE_KEYS[cycle][0]]


def webhook(db, call, status="successful", amount=None, currency="USD", tx_id=777):
    payload = {"event": "charge.completed", "data": {"id": tx_id, "tx_ref": call["tx_ref"], "status": status,
               "amount": call["amount"] if amount is None else amount, "currency": currency, "meta": call["meta"], "customer": {"id": 5}}}
    return billing.handle_flutterwave_webhook(db, json.dumps(payload).encode(), "test-hash")


# ---------------------------------------------------------------- without a code, nothing changes
def test_without_a_code_the_original_recurring_charge_is_unchanged(client, db, fw):
    r = client.post("/api/billing/pending-checkout", json=body())
    assert r.status_code == 200 and r.json()["checkout_url"] == "https://pay.test/checkout"
    call = fw[0]
    assert call["amount"] == catalog_price(db)
    assert call["payment_plan"] == billing.get_plan_catalog(db)["growth"][billing.CYCLE_KEYS["annual"][3]]   # still the recurring plan
    assert "promo_percent" not in call["meta"]


# ---------------------------------------------------------------- with a code
def test_a_code_takes_15_percent_off_and_is_charged_as_a_one_time_year(client, db, fw):
    seed_valid_codes(db)
    r = client.post("/api/billing/pending-checkout", json=body(promo_code="ph15", utm_source="producthunt", utm_medium="launch",
                                                              utm_campaign="official_launch", session_id="sess_abcdef123456"))
    assert r.status_code == 200
    base = catalog_price(db)
    call = fw[0]
    assert call["amount"] == round(base - base * 0.15, 2)
    assert call["payment_plan"] is None                  # a fixed recurring plan cannot be discounted, so it is a one-time prepaid year
    assert call["meta"]["promo_percent"] == 15
    row = db.scalar(select(SignupAttribution))
    assert (row.status, row.promo_code, row.discount_percent) == ("started", "PH15", 15)
    assert row.discount_amount == round(base * 0.15, 2) and row.charged_amount == call["amount"] and row.currency == "USD"
    assert (row.utm_source, row.utm_medium, row.utm_campaign, row.session_id) == ("producthunt", "launch", "official_launch", "sess_abcdef123456")


def test_a_code_works_when_paying_in_naira(client, db, fw):
    seed_valid_codes(db)
    assert client.post("/api/billing/pending-checkout", json=body(promo_code="HN15", pay_in_ngn=True)).status_code == 200
    call = fw[0]
    rate = billing.get_ngn_rate(db)
    base = catalog_price(db)
    assert call["currency"] == "NGN" and call["amount"] == round((base - round(base * 0.15, 2)) * rate, 2)
    assert db.scalar(select(SignupAttribution)).currency == "NGN"


def test_buying_three_years_discounts_only_the_first(client, db, fw):
    seed_valid_codes(db)
    assert client.post("/api/billing/pending-checkout", json=body(promo_code="IH15", quantity=3)).status_code == 200
    base = catalog_price(db)
    assert fw[0]["amount"] == round(base * 3 - base * 0.15, 2)


def test_an_unusable_code_is_refused_with_the_reason_and_creates_nothing(client, db, fw):
    seed_valid_codes(db)
    r = client.post("/api/billing/pending-checkout", json=body(promo_code="WRONG"))
    assert r.status_code == 422 and "isn't valid" in r.json()["detail"]
    assert fw == [] and db.scalars(select(PendingSignup)).all() == []


def test_the_offer_is_refused_for_monthly_billing(client, db, fw):
    seed_valid_codes(db)
    r = client.post("/api/billing/pending-checkout", json=body(promo_code="PH15", cycle="monthly"))
    assert r.status_code == 422 and "annual" in r.json()["detail"]
    assert fw == []


def test_campaign_tags_are_recorded_even_without_a_code(client, db, fw):
    assert client.post("/api/billing/pending-checkout", json=body(utm_source="HackerNews", utm_campaign="official_launch")).status_code == 200
    row = db.scalar(select(SignupAttribution))
    assert row.utm_source == "hackernews" and row.promo_code == "" and row.discount_amount == 0.0


def test_a_problem_in_the_launch_report_can_never_stop_a_customer_paying(client, db, fw, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("report database trouble")
    monkeypatch.setattr(promos, "record_started", explode)
    r = client.post("/api/billing/pending-checkout", json=body())
    assert r.status_code == 200 and r.json()["checkout_url"] == "https://pay.test/checkout"
    pending = db.scalar(select(PendingSignup))
    assert pending is not None and pending.tx_ref == fw[0]["tx_ref"]      # the payment link is saved regardless


# ---------------------------------------------------------------- when the payment is confirmed
def test_a_confirmed_payment_creates_the_account_and_marks_the_offer_used(client, db, fw):
    seed_valid_codes(db)
    client.post("/api/billing/pending-checkout", json=body(promo_code="PH15", utm_source="producthunt"))
    webhook(db, fw[0])
    org = db.scalar(select(Organization).where(Organization.name == "Ada Co"))
    assert org is not None and org.billing_status == "active"
    row = db.scalar(select(SignupAttribution))
    assert row.status == "paid" and row.organization_id == org.id and row.paid_at is not None
    from app.models import PromoCode
    assert promos.redemptions(db, db.scalar(select(PromoCode).where(PromoCode.code == "PH15"))) == 1


def test_a_retried_webhook_never_double_counts(client, db, fw):
    seed_valid_codes(db)
    client.post("/api/billing/pending-checkout", json=body(promo_code="PH15"))
    webhook(db, fw[0])
    webhook(db, fw[0])      # Flutterwave retries webhooks; the second must change nothing
    assert len(db.scalars(select(Organization).where(Organization.name == "Ada Co")).all()) == 1
    rows = db.scalars(select(SignupAttribution)).all()
    assert len(rows) == 1 and rows[0].status == "paid"


def test_a_failed_payment_creates_no_account_and_does_not_use_the_offer(client, db, fw):
    seed_valid_codes(db)
    client.post("/api/billing/pending-checkout", json=body(promo_code="PH15"))
    webhook(db, fw[0], status="failed")
    assert db.scalar(select(Organization).where(Organization.name == "Ada Co")) is None
    assert db.scalar(select(SignupAttribution)).status == "started"


def test_a_webhook_with_the_wrong_secret_is_refused(client, db, fw):
    client.post("/api/billing/pending-checkout", json=body())
    payload = json.dumps({"event": "charge.completed", "data": {"id": 1, "tx_ref": fw[0]["tx_ref"], "status": "successful", "amount": 1,
                          "currency": "USD", "meta": fw[0]["meta"]}}).encode()
    with pytest.raises(PermissionError):
        billing.handle_flutterwave_webhook(db, payload, "not-the-secret")
    assert db.scalar(select(Organization).where(Organization.name == "Ada Co")) is None


def test_a_limited_offer_closes_after_the_last_paid_signup(client, db, fw):
    from app.models import PromoCode
    db.add(PromoCode(code="ONLY1", percent=15, platform="Test", max_redemptions=1))
    db.commit()
    client.post("/api/billing/pending-checkout", json=body(promo_code="ONLY1"))
    webhook(db, fw[0])
    second = client.post("/api/billing/pending-checkout", json=body(email="bola@example.com", company="Bola Co", promo_code="ONLY1"))
    assert second.status_code == 422 and "fully redeemed" in second.json()["detail"]


def test_a_discounted_customer_gets_a_full_year_of_access(client, db, fw):
    from datetime import timedelta
    from app.models import aware, now_utc
    seed_valid_codes(db)
    client.post("/api/billing/pending-checkout", json=body(promo_code="PH15"))
    webhook(db, fw[0])
    org = db.scalar(select(Organization).where(Organization.name == "Ada Co"))
    days = (aware(org.paid_until) - now_utc()).days
    assert 363 <= days <= 365
