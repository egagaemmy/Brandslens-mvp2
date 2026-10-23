"""The launch offer: the rules and the maths. This is real money, so the rules are tested one by one."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import PromoCode, SignupAttribution
from app.services import billing, promos
from tests.helpers import seed_valid_codes


# ---------------------------------------------------------------- the maths
def test_fifteen_percent_off_one_year():
    assert promos.price_with_promo(1500, 1, 15) == (225.0, 1275.0)


def test_only_the_first_year_is_discounted_when_buying_several_years():
    discount, total = promos.price_with_promo(1500, 3, 15)
    assert discount == 225.0                 # 15% of ONE year's price, never of three
    assert total == 4500 - 225.0


@pytest.mark.parametrize("price,pct", [(99.99, 15), (1234.57, 15), (0.01, 15), (2500, 20)])
def test_amounts_are_rounded_to_the_cent_and_never_negative(price, pct):
    discount, total = promos.price_with_promo(price, 1, pct)
    assert discount == round(discount, 2) and total == round(total, 2)
    assert total >= 0 and abs((discount + total) - price) < 0.011


def test_no_discount_when_percent_is_zero():
    assert promos.price_with_promo(1500, 2, 0) == (0.0, 3000.0)


# ---------------------------------------------------------------- code handling
@pytest.mark.parametrize("typed,expected", [(" ph15 ", "PH15"), ("ph-15", "PH15"), ("Hn 15!", "HN15"), (None, ""), ("", "")])
def test_codes_are_forgiving_about_how_people_type_them(typed, expected):
    assert promos.normalise(typed) == expected


def test_campaign_tags_are_cleaned():
    assert promos.clean_tag("ProductHunt ") == "producthunt"
    assert promos.clean_tag("<script>x</script>") == "scriptxscript"
    assert len(promos.clean_tag("a" * 500)) == 60


# ---------------------------------------------------------------- when a code may be used
def test_a_valid_code_works(db):
    seed_valid_codes(db)
    assert promos.get_valid(db, "ph15", "growth", "annual").percent == 15


def test_an_unknown_code_is_refused(db):
    seed_valid_codes(db)
    with pytest.raises(promos.PromoError, match="isn't valid"):
        promos.get_valid(db, "NOPE", "growth", "annual")


def test_an_empty_code_is_refused(db):
    with pytest.raises(promos.PromoError, match="Enter a code"):
        promos.get_valid(db, "  ", "growth", "annual")


def test_an_inactive_code_is_refused(db):
    seed_valid_codes(db)
    db.query(PromoCode).filter_by(code="PH15").update({"active": False})
    db.commit()
    with pytest.raises(promos.PromoError, match="isn't valid"):
        promos.get_valid(db, "PH15", "growth", "annual")


def test_a_code_cannot_be_used_before_its_start(db):
    db.add(PromoCode(code="SOON15", percent=15, valid_from=datetime.now(timezone.utc) + timedelta(days=2)))
    db.commit()
    with pytest.raises(promos.PromoError, match="hasn't started"):
        promos.get_valid(db, "SOON15", "growth", "annual")


def test_a_code_cannot_be_used_after_its_end(db):
    db.add(PromoCode(code="OLD15", percent=15, valid_until=datetime.now(timezone.utc) - timedelta(minutes=1)))
    db.commit()
    with pytest.raises(promos.PromoError, match="has ended"):
        promos.get_valid(db, "OLD15", "growth", "annual")


@pytest.mark.parametrize("cycle", ["monthly", "daily"])
def test_the_offer_is_annual_only(db, cycle):
    seed_valid_codes(db)
    with pytest.raises(promos.PromoError, match="annual"):
        promos.get_valid(db, "PH15", "growth", cycle)


def test_only_paid_signups_use_up_a_limited_offer(db):
    db.add(PromoCode(code="LIMIT15", percent=15, max_redemptions=1))
    db.commit()
    # an abandoned checkout must NOT use the offer up
    db.add(SignupAttribution(pending_signup_id="p1", promo_code="LIMIT15", status="started"))
    db.commit()
    assert promos.get_valid(db, "LIMIT15", "growth", "annual").code == "LIMIT15"
    # a paid signup does
    db.add(SignupAttribution(pending_signup_id="p2", promo_code="LIMIT15", status="paid"))
    db.commit()
    with pytest.raises(promos.PromoError, match="fully redeemed"):
        promos.get_valid(db, "LIMIT15", "growth", "annual")


# ---------------------------------------------------------------- seeding the launch codes
def test_seeding_creates_the_four_launch_codes_from_midnight_lagos(db):
    out = promos.seed_launch_codes(db, percent=15, starts_on="2026-11-24", days=31)
    assert sorted(out["created"]) == ["HN15", "IH15", "PEERLIST15", "PH15"]
    ph = db.query(PromoCode).filter_by(code="PH15").one()
    assert ph.percent == 15 and ph.platform == "Product Hunt"
    # midnight on 24 November in Lagos (UTC+1) is 23:00 UTC on the 23rd
    assert ph.valid_from.replace(tzinfo=timezone.utc) == datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc)
    assert ph.valid_until.replace(tzinfo=timezone.utc) == datetime(2026, 12, 24, 23, 0, tzinfo=timezone.utc)


def test_seeding_twice_never_overwrites_an_edited_code(db):
    promos.seed_launch_codes(db)
    db.query(PromoCode).filter_by(code="HN15").update({"percent": 25, "active": False})
    db.commit()
    again = promos.seed_launch_codes(db)
    assert again["created"] == [] and sorted(again["already_existed"]) == ["HN15", "IH15", "PEERLIST15", "PH15"]
    hn = db.query(PromoCode).filter_by(code="HN15").one()
    assert hn.percent == 25 and hn.active is False


# ---------------------------------------------------------------- the price a customer is charged
def test_no_promo_reproduces_the_original_amounts_exactly(db):
    for plan in ("standard", "growth", "professional"):
        for cycle, qty in (("annual", 1), ("annual", 3), ("monthly", 2), ("daily", 5)):
            base = billing.get_plan_catalog(db)[plan][billing.CYCLE_KEYS[cycle][0]]
            assert billing.signup_pricing(db, plan, cycle, qty)["amount"] == round(base * qty, 2)
            assert billing.signup_pricing(db, plan, cycle, qty)["discount"] == 0.0
            rate = billing.get_ngn_rate(db)
            assert billing.signup_pricing(db, plan, cycle, qty, pay_in_ngn=True)["amount"] == round(base * qty * rate, 2)


def test_promo_pricing_in_dollars_and_naira(db):
    base = billing.get_plan_catalog(db)["growth"][billing.CYCLE_KEYS["annual"][0]]
    usd = billing.signup_pricing(db, "growth", "annual", 1, promo_percent=15)
    assert usd["currency"] == "USD" and usd["discount"] == round(base * 0.15, 2) and usd["amount"] == round(base - base * 0.15, 2)
    ngn = billing.signup_pricing(db, "growth", "annual", 1, promo_percent=15, pay_in_ngn=True)
    rate = billing.get_ngn_rate(db)
    assert ngn["currency"] == "NGN" and ngn["amount"] == round((base - round(base * 0.15, 2)) * rate, 2)


def test_quote_reports_what_the_customer_will_see(db):
    seed_valid_codes(db)
    q = promos.quote(db, "hn15", "growth", "annual", 1, 2000.0)
    assert q == {"valid": True, "code": "HN15", "percent": 15, "platform": "Hacker News", "discount": 300.0, "total": 1700.0,
                 "full_total": 2000.0, "message": "15% off your first year."}
