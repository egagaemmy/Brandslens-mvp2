"""Weekly scorecard: the week boundaries, the counts by channel, the accuracy checks, sending once, and the admin preview."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import BrandCheck, SignupAttribution, TrialSignup
from app.services import scorecard
from app.services.settings import get_setting

LAGOS = timezone(timedelta(hours=1))
# Monday 30 Nov 2026, 10:00 Lagos: the last full week is Mon 23 to Sun 29 Nov
NOW = datetime(2026, 11, 30, 10, 0, tzinfo=LAGOS)
IN_WEEK = datetime(2026, 11, 25, 12, 0, tzinfo=LAGOS)


def _bc(db, email, src="", med="", status="done", when=IN_WEEK, emailed=True, cached=None):
    r = BrandCheck(email=email, email_domain=email.split("@")[1], brand="Acme", website="acme.com", status=status,
                   utm_source=src, utm_medium=med, created_at=when, emailed_at=when if emailed else None, cached_from=cached)
    db.add(r)
    return r


def _trial(db, email, src="", med="", started=IN_WEEK, created=None):
    r = TrialSignup(email=email, domain=email.split("@")[1], status="active", utm_source=src, utm_medium=med,
                    created_at=created or started or IN_WEEK, started_at=started)
    db.add(r)
    return r


def test_week_bounds_are_monday_to_sunday_lagos():
    start, end, monday = scorecard.week_bounds(NOW)
    assert monday.isoformat() == "2026-11-23"
    assert start == datetime(2026, 11, 22, 23, 0, tzinfo=timezone.utc)
    assert end - start == timedelta(days=7)


def test_week_bounds_on_a_sunday_still_points_at_the_previous_full_week():
    _, _, monday = scorecard.week_bounds(datetime(2026, 11, 29, 23, 30, tzinfo=LAGOS))
    assert monday.isoformat() == "2026-11-16"


def test_counts_by_channel_and_from_brand_check(db):
    _bc(db, "a@one.com", "linkedin", "company")
    _bc(db, "b@two.com", "linkedin", "company", status="failed", emailed=False)
    _bc(db, "c@three.com", "x", "social", cached="other-id")
    _bc(db, "d@four.com")                                                           # untagged
    _bc(db, "old@five.com", "x", "social", when=IN_WEEK - timedelta(days=10))      # previous week: not counted
    _bc(db, "me@brandslens.com", "linkedin", "company")                             # our own team: left out
    _trial(db, "a@one.com", "linkedin", "company", started=IN_WEEK + timedelta(hours=2))   # checked first, then trial
    _trial(db, "z@zed.com", "reddit", "community")
    _trial(db, "late@six.com", "x", "social", started=None, created=IN_WEEK)        # requested, not started
    db.add(SignupAttribution(pending_signup_id="p1", email="a@one.com", utm_source="linkedin", utm_medium="company",
                             status="paid", paid_at=IN_WEEK))
    db.commit()
    card = scorecard.build(db, NOW)
    bc = card["brand_checks"]
    assert bc["total"] == 4 and bc["team"] == 1
    assert bc["by_channel"] == {"linkedin / company": 2, "x / social": 1, "untagged": 1}
    assert (bc["done"], bc["failed"], bc["emailed"], bc["reused"]) == (3, 1, 3, 1)
    assert card["trial_requests"] == 3
    assert card["trials"]["total"] == 2 and card["trials"]["from_brand_check"] == 1
    assert card["trials"]["by_channel"] == {"linkedin / company": 1, "reddit / community": 1}
    assert card["paid"]["total"] == 1
    assert card["checks_passed"] and card["problems"] == []


def test_trial_before_the_brand_check_does_not_count_as_from_brand_check(db):
    _bc(db, "a@one.com", when=IN_WEEK + timedelta(hours=5))
    _trial(db, "a@one.com", started=IN_WEEK)
    db.commit()
    assert scorecard.build(db, NOW)["trials"]["from_brand_check"] == 0


def test_plan_position_and_targets():
    from datetime import date
    assert scorecard.plan_position(date(2026, 11, 17)) == (0, 0)
    assert scorecard.plan_position(date(2026, 11, 18)) == (1, 0)
    assert scorecard.plan_position(date(2026, 11, 24)) == (1, 150)        # week 1 (Wed 18 to Tue 24) finished
    assert scorecard.plan_position(date(2026, 11, 29)) == (2, 150)
    assert scorecard.plan_position(date(2026, 12, 1)) == (2, 650)
    assert scorecard.plan_position(date(2027, 3, 1))[1] == 5000


def test_render_has_no_dashes_in_prose_and_shows_period(db):
    _trial(db, "z@zed.com", "reddit", "community")
    db.commit()
    subject, body = scorecard.render(scorecard.build(db, NOW))
    assert "Mon 23 Nov to Sun 29 Nov" in subject
    assert "All figures were checked" in body
    assert "—" not in body and "–" not in body


def test_problems_are_shown_at_the_top(db):
    card = scorecard.build(db, NOW)
    card["problems"] = ["Trials started: counted 3 records but the database total is 4."]
    _, body = scorecard.render(card)
    assert "Please read before using these numbers" in body and "database total is 4" in body


def test_recipients(monkeypatch):
    monkeypatch.delenv("SCORECARD_TO", raising=False)
    assert scorecard.recipients() == ["brandslenslimited@gmail.com"]
    monkeypatch.setenv("SCORECARD_TO", "off")
    assert scorecard.recipients() == []
    monkeypatch.setenv("SCORECARD_TO", "a@x.com, bad, b@y.org")
    assert scorecard.recipients() == ["a@x.com", "b@y.org"]


def test_sends_once_per_week_and_not_before_nine_on_monday(db, monkeypatch):
    sent = []
    monkeypatch.delenv("SCORECARD_TO", raising=False)
    monkeypatch.setattr("app.services.mailer.send_email", lambda to, s, b, from_addr="", reply_to="": sent.append((to, s, from_addr)) or True)
    assert scorecard.send_if_due(db, datetime(2026, 11, 30, 8, 30, tzinfo=LAGOS)) is False and sent == []
    assert scorecard.send_if_due(db, NOW) is True
    assert sent[0][0] == "brandslenslimited@gmail.com" and sent[0][2].startswith("BrandsLens Scorecard <")
    assert get_setting(db, scorecard.SENT_KEY) == "2026-11-23"
    assert scorecard.send_if_due(db, NOW + timedelta(hours=1)) is False and len(sent) == 1       # never twice
    assert scorecard.send_if_due(db, NOW + timedelta(days=7)) is True and len(sent) == 2          # next week goes out


def test_catches_up_later_in_the_week_after_a_missed_monday(db, monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.mailer.send_email", lambda *a, **k: sent.append(a) or True)
    assert scorecard.send_if_due(db, datetime(2026, 12, 1, 14, 0, tzinfo=LAGOS)) is True
    assert get_setting(db, scorecard.SENT_KEY) == "2026-11-23"


def test_failed_send_is_retried_next_hour(db, monkeypatch):
    monkeypatch.setattr("app.services.mailer.send_email", lambda *a, **k: False)
    assert scorecard.send_if_due(db, NOW) is False
    assert get_setting(db, scorecard.SENT_KEY, "") == ""


def test_off_sends_nothing(db, monkeypatch):
    sent = []
    monkeypatch.setenv("SCORECARD_TO", "off")
    monkeypatch.setattr("app.services.mailer.send_email", lambda *a, **k: sent.append(a) or True)
    assert scorecard.send_if_due(db, NOW) is False and sent == []


def test_admin_preview_is_team_only_and_sends_nothing(client, db, monkeypatch):
    from tests.helpers import bearer, make_admin, make_customer
    sent = []
    monkeypatch.setattr("app.services.mailer.send_email", lambda *a, **k: sent.append(a) or True)
    _, ctoken, _ = make_customer(db)
    assert client.get("/api/admin/scorecard", headers=bearer(ctoken)).status_code == 403
    _, token = make_admin(db)
    r = client.get("/api/admin/scorecard", headers=bearer(token))
    assert r.status_code == 200
    j = r.json()
    assert j["scorecard"]["checks_passed"] is True and "Weekly scorecard" in j["html"] and j["recipients"]
    assert sent == [] and get_setting(db, scorecard.SENT_KEY, "") == ""


def test_worker_job_runs_cleanly():
    from app import worker
    worker.weekly_scorecard()
