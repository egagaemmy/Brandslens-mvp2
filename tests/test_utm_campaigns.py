"""Campaign tags (source, medium, campaign, term, content): stored tidied, carried through checkout, and reported per campaign."""
from sqlalchemy import select

from app.models import LaunchEvent, SignupAttribution
from app.services import promos


def track(client, **kw):
    body = {"kind": "visit", "session_id": "sess0001abcd", "path": "/launch", "utm_source": "producthunt", "utm_medium": "launch", "utm_campaign": "official_launch", **kw}
    return client.post("/api/public/track", json=body)


def test_term_and_content_are_stored_tidied_like_the_other_tags(client, db):
    assert track(client, utm_term="AI Tool!", utm_content="Banner A").status_code == 204
    row = db.scalars(select(LaunchEvent)).one()
    assert (row.utm_term, row.utm_content) == ("aitool", "bannera")


def test_overlong_or_hostile_term_and_content_are_cut_and_cleaned(client, db):
    track(client, utm_term="x" * 200, utm_content="<script>alert(1)</script>")
    row = db.scalars(select(LaunchEvent)).one()
    assert len(row.utm_term) == 80 and "<" not in row.utm_content and ">" not in row.utm_content and "(" not in row.utm_content


def test_a_visit_without_term_or_content_is_still_recorded(client, db):
    assert track(client).status_code == 204
    row = db.scalars(select(LaunchEvent)).one()
    assert (row.utm_term, row.utm_content) == ("", "")


def test_the_campaign_tags_survive_all_the_way_to_a_paid_signup(db):
    utm = {"source": "producthunt", "medium": "launch", "campaign": "official_launch", "term": "Media Monitoring", "content": "banner_a"}
    promos.record_started(db, "pend1", "a@example.com", "growth", "annual", utm, "sess0001abcd", None, 0, 2125, "USD")
    db.commit()
    promos.mark_paid(db, "pend1", "org1", 2125, "USD")
    row = db.scalars(select(SignupAttribution)).one()
    assert (row.utm_term, row.utm_content, row.status) == ("mediamonitoring", "banner_a", "paid")


def test_two_links_from_the_same_platform_are_reported_separately(db):
    for sid, campaign, medium in (("sessaaaa0001", "official_launch", "launch"), ("sessaaaa0002", "official_launch", "launch"), ("sessbbbb0001", "teaser", "social")):
        promos.record_event(db, promos.clean_event("visit", sid, "producthunt", medium, campaign, "/launch"))
    promos.record_event(db, promos.clean_event("demo_start", "sessaaaa0001", "producthunt", "launch", "official_launch", "/demo"))
    promos.record_started(db, "pendA", "a@example.com", "growth", "annual", {"source": "producthunt", "medium": "launch", "campaign": "official_launch"}, "sessaaaa0001", None, 0, 2125, "USD")
    db.commit()
    promos.mark_paid(db, "pendA", "org1", 2125, "USD")
    report = promos.launch_summary(db)
    by = {(c["medium"], c["campaign"]): c for c in report["campaigns"]}
    assert set(by) == {("launch", "official_launch"), ("social", "teaser")}
    assert (by[("launch", "official_launch")]["visitors"], by[("launch", "official_launch")]["demo_starts"], by[("launch", "official_launch")]["paid"]) == (2, 1, 1)
    assert by[("launch", "official_launch")]["revenue"] == {"USD": 2125.0}
    assert (by[("social", "teaser")]["visitors"], by[("social", "teaser")]["paid"]) == (1, 0)
    assert [c["campaign"] for c in report["campaigns"]] == ["official_launch", "teaser"]                       # the one that earned money is listed first
    assert len(report["sources"]) == 1 and report["sources"][0]["visitors"] == 3                               # and the platform total still adds them up


def test_visits_with_no_campaign_are_grouped_as_direct(db):
    promos.record_event(db, promos.clean_event("visit", "sessdirect001", "", "", "", "/"))
    report = promos.launch_summary(db)
    assert report["campaigns"][0]["source"] == "(direct)" and report["sources"][0]["source"] == "(direct)"
