"""The free Brand Exposure Check: switch, business emails only, limits, the queue, the results page, the email, the cache and Super Admin.
No test reaches the network: the news search, the domain check and the rating are replaced."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import BrandCheck, now_utc
from app.services import brand_check
from tests.helpers import bearer, make_admin, make_customer

NEWS = [{"title": "Acme Foods opens new plant", "url": "https://news.example/a", "source": "news.example", "date": "2026-11-01"},
        {"title": "Fake Acme Foods giveaway asks fans to pay a fee", "url": "https://news.example/b", "source": "news.example", "date": "2026-11-10"},
        {"title": "<script>alert(1)</script> Acme Foods recall", "url": "https://news.example/c", "source": "news.example", "date": "2026-11-12"}]
RATED = {"Fake": "HIGH", "recall": "MEDIUM"}


@pytest.fixture(autouse=True)
def fake_sources(monkeypatch):
    calls = {"news": 0, "domains": 0, "rate": 0, "emails": [], "mailchimp": []}

    def news(brand, website):
        calls["news"] += 1
        return [dict(n) for n in NEWS]

    def domains(website):
        calls["domains"] += 1
        return ["acmefoods-offers.com", "acme-foods-support.com"]

    def rate(brand, website, items):
        calls["rate"] += 1
        return [next((v for k, v in RATED.items() if k in it["title"]), "WATCH") for it in items]

    monkeypatch.setattr(brand_check, "fetch_news", news)
    monkeypatch.setattr(brand_check, "find_domains", domains)
    monkeypatch.setattr(brand_check, "rate_news", rate)
    from app.services import mailer
    monkeypatch.setattr(mailer, "send_email", lambda to, subject, body, **kw: calls["emails"].append((to, subject, body)) or True)
    monkeypatch.setattr(mailer, "add_to_mailchimp", lambda email, name="": calls["mailchimp"].append(email) or True)
    from app import routes_brand_check
    routes_brand_check._start_ip._hits.clear()
    routes_brand_check._result_ip._hits.clear()
    return calls


def _on(db, **kw):
    brand_check.save_settings(db, "admin@brandslens.test", {"enabled": True, **kw})


def _start(client, email="pat@acmefoods.com", brand="Acme Foods", website="https://www.acmefoods.com/shop", **kw):
    return client.post("/api/public/brand-check", json={"email": email, "brand": brand, "website": website, **kw})


# ------------------------------------------------------------------ switch and input
def test_off_by_default(client, db):
    assert client.get("/api/public/brand-check/status").json()["open"] is False
    r = _start(client)
    assert r.status_code == 503
    assert db.scalar(select(BrandCheck)) is None


def test_personal_and_throwaway_emails_are_refused(client, db):
    _on(db)
    r = _start(client, email="someone@gmail.com")
    assert r.status_code == 422 and "work email" in r.json()["detail"] and "trial" not in r.json()["detail"]
    assert _start(client, email="x@mailinator.com").status_code == 422
    assert _start(client, email="not-an-email").status_code == 422


@pytest.mark.parametrize("raw,clean", [("https://www.Acme-Foods.com/shop?x=1", "acme-foods.com"), ("acmefoods.co.uk", "acmefoods.co.uk"),
                                       ("http://shop.acme.ng:8080/", "shop.acme.ng"), ("  WWW.ACME.COM  ", "acme.com")])
def test_website_is_normalised(raw, clean):
    assert brand_check.normalise_website(raw) == clean


@pytest.mark.parametrize("bad", ["", "acme", "http://", "192.168.0.1", "acme..com", "-acme.com", "a" * 300 + ".com", "javascript:alert(1)"])
def test_bad_websites_are_refused(bad):
    with pytest.raises(brand_check.BrandCheckError):
        brand_check.normalise_website(bad)


def test_brand_name_is_required(client, db):
    _on(db)
    assert _start(client, brand=" ").status_code == 422


# ------------------------------------------------------------------ the queue and the results
def test_a_check_is_queued_then_run_by_the_worker(client, db, fake_sources):
    _on(db)
    r = _start(client, utm_source="linkedin", utm_campaign="Brand Check!")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and body["queue_position"] == 1 and body["website"] == "acmefoods.com"
    assert body["news"]["ready"] is False and body["domains"]["ready"] is False
    assert fake_sources["news"] == 0                                 # nothing runs inside the web request

    assert brand_check.process_queue(db) == 1
    view = client.get(f"/api/public/brand-check/{body['id']}", params={"t": body["token"]}).json()
    assert view["status"] == "done"
    assert view["news"]["total"] == 3 and view["news"]["attention"] == 2
    assert [n["severity"] for n in view["news"]["items"]] == ["HIGH", "MEDIUM", "WATCH"]
    assert view["domains"]["total"] == 2 and "acmefoods-offers.com" in view["domains"]["items"]
    assert "utm_source=linkedin" in view["trial_url"] and "utm_content=brand-check" in view["trial_url"] and "email=pat%40acmefoods.com" in view["trial_url"]
    row = db.get(BrandCheck, body["id"])
    assert row.utm_campaign == "brandcheck" and row.ip_hash and row.emailed_at is not None
    assert view["emailed"] is True


def test_results_need_the_secret_token(client, db):
    _on(db)
    body = _start(client).json()
    assert client.get(f"/api/public/brand-check/{body['id']}", params={"t": "wrong"}).status_code == 404
    assert client.get(f"/api/public/brand-check/{body['id']}", params={"t": ""}).status_code == 404
    assert client.get("/api/public/brand-check/nope", params={"t": body["token"]}).status_code == 404


def test_news_shows_before_the_websites_finish(client, db, monkeypatch):
    _on(db)
    body = _start(client).json()
    seen = {}

    def slow_domains(website):
        seen["view"] = client.get(f"/api/public/brand-check/{body['id']}", params={"t": body["token"]}).json()
        return []
    monkeypatch.setattr(brand_check, "find_domains", slow_domains)
    brand_check.process_queue(db)
    assert seen["view"]["status"] == "running"
    assert seen["view"]["news"]["ready"] is True and seen["view"]["news"]["total"] == 3
    assert seen["view"]["domains"]["ready"] is False


def test_a_failed_part_says_unavailable_never_nothing_found(client, db, monkeypatch, fake_sources):
    _on(db)
    body = _start(client).json()

    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(brand_check, "fetch_news", boom)
    monkeypatch.setattr(brand_check, "find_domains", boom)
    brand_check.process_queue(db)
    view = client.get(f"/api/public/brand-check/{body['id']}", params={"t": body["token"]}).json()
    assert view["status"] == "done"
    assert view["news"]["unavailable"] is True and view["news"]["items"] == []
    assert view["domains"]["unavailable"] is True and view["domains"]["items"] == []
    html = fake_sources["emails"][0][2]
    assert "could not run" in html and "no registered addresses" not in html


def test_two_workers_never_run_the_same_check(db):
    _on(db)
    row = brand_check.start(db, email="pat@acmefoods.com", brand="Acme Foods", website="acmefoods.com")
    now = now_utc()
    assert brand_check._claim(db, row.id, now) is True
    assert brand_check._claim(db, row.id, now) is False


def test_an_interrupted_check_is_retried_once_then_marked_failed(db):
    _on(db)
    row = brand_check.start(db, email="pat@acmefoods.com", brand="Acme Foods", website="acmefoods.com")
    later = now_utc() + timedelta(minutes=brand_check.STALE_MINUTES + 1)
    row.status, row.started_at, row.attempts = "running", now_utc(), 1
    db.commit()
    brand_check.process_queue(db, limit=0, now=later)
    db.refresh(row)
    assert row.status == "queued"
    row.status, row.started_at, row.attempts = "running", now_utc(), 2
    db.commit()
    brand_check.process_queue(db, limit=0, now=later)
    db.refresh(row)
    assert row.status == "failed"


# ------------------------------------------------------------------ the email and the lead
def test_results_email_is_sent_once_escaped_and_only_names_what_we_scan(client, db, fake_sources):
    _on(db)
    _start(client)
    brand_check.process_queue(db)
    brand_check.process_queue(db)
    assert len(fake_sources["emails"]) == 1
    to, subject, html = fake_sources["emails"][0]
    assert to == "pat@acmefoods.com" and "Acme Foods" in subject
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "acmefoods-offers.com" in html and "Start your free trial" in html and "Pat from BrandsLens" in html
    for unscanned in ("Instagram", "Facebook", "TikTok", "LinkedIn"):
        assert unscanned not in html


def test_mailchimp_only_with_consent(client, db, fake_sources):
    _on(db)
    _start(client, email="a@acmefoods.com")
    _start(client, email="b@acmefoods.com", website="acme2.com", marketing_consent=True)
    brand_check.process_queue(db, limit=5)
    assert fake_sources["mailchimp"] == ["b@acmefoods.com"]


# ------------------------------------------------------------------ cost controls
def test_same_brand_within_a_day_reuses_the_result(client, db, fake_sources):
    _on(db)
    _start(client, email="a@acmefoods.com")
    brand_check.process_queue(db)
    second = _start(client, email="b@agency.com", brand="acme foods", website="acmefoods.com").json()
    assert second["status"] == "done" and second["news"]["total"] == 3
    assert fake_sources["news"] == 1 and fake_sources["domains"] == 1
    assert len(fake_sources["emails"]) == 2                         # the second person still gets their own email
    assert db.get(BrandCheck, second["id"]).cached_from


def test_an_old_result_is_not_reused(client, db, fake_sources):
    _on(db)
    first = _start(client, email="a@acmefoods.com").json()
    brand_check.process_queue(db)
    row = db.get(BrandCheck, first["id"])
    row.finished_at = now_utc() - timedelta(hours=brand_check.CACHE_HOURS + 1)
    db.commit()
    assert _start(client, email="b@acmefoods.com").json()["status"] == "queued"


def test_limits_per_email_per_company_and_per_day(client, db):
    _on(db, per_email=2, per_company=3, daily_cap=4)
    assert _start(client, email="a@acme.com", website="one.com").status_code == 200
    assert _start(client, email="a@acme.com", website="two.com").status_code == 200
    r = _start(client, email="a@acme.com", website="three.com")
    assert r.status_code == 429 and "free trial" in r.json()["detail"]
    assert _start(client, email="b@acme.com", website="four.com").status_code == 200
    assert _start(client, email="c@acme.com", website="five.com").status_code == 429           # company limit
    assert _start(client, email="d@other.com", website="six.com").status_code == 200
    assert _start(client, email="e@third.com", website="seven.com").status_code == 429         # daily cap


def test_connection_limit(client, db):
    _on(db, per_email=20, per_company=100, daily_cap=1000)
    codes = [_start(client, email=f"p{i}@co{i}.com", website=f"site{i}.com").status_code for i in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


# ------------------------------------------------------------------ Super Admin
def test_only_the_team_can_change_settings_or_see_leads(client, db):
    _, token, _ = make_customer(db)
    assert client.get("/api/admin/brand-check/settings", headers=bearer(token)).status_code == 403
    assert client.get("/api/admin/brand-check/checks", headers=bearer(token)).status_code == 403
    assert client.get("/api/admin/brand-check/export.csv", headers=bearer(token)).status_code == 403


def test_admin_switches_it_on_and_sees_the_leads(client, db):
    _, token = make_admin(db)
    r = client.put("/api/admin/brand-check/settings", headers=bearer(token), json={"enabled": True, "daily_cap": 999999, "per_email": 0})
    s = r.json()["settings"]
    assert s["enabled"] is True and s["daily_cap"] == 5000 and s["per_email"] == 1
    assert client.get("/api/public/brand-check/status").json()["open"] is True
    _start(client, email="=cmd@acme.com")
    listing = client.get("/api/admin/brand-check/checks", headers=bearer(token), params={"q": "acme"}).json()
    assert listing["total"] == 1 and listing["items"][0]["brand"] == "Acme Foods"
    csv_text = client.get("/api/admin/brand-check/export.csv", headers=bearer(token)).text
    assert csv_text.splitlines()[0].startswith("created_at,email,brand")
    assert "'=cmd@acme.com" in csv_text
    summary = client.get("/api/admin/brand-check/settings", headers=bearer(token)).json()["summary"]
    assert summary["today"] == 1 and summary["waiting"] == 1


def test_worker_job_runs_cleanly_with_nothing_waiting():
    from app import worker
    worker.brand_checks()


def test_the_real_news_helper_reads_the_news_search(monkeypatch):
    import importlib
    real = importlib.reload(brand_check)          # the fixture replaced fetch_news on the module; get the real one back
    seen = {}

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"articles": [{"title": "Acme  Foods\n wins award", "url": "https://n.example/x", "domain": "n.example",
                                              "seendate": "20261110T120000Z"}, {"title": ""}]}
    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, params=None, timeout=None: seen.update(params) or R())
    out = real.fetch_news("Acme Foods", "acmefoods.com")
    assert seen["query"] == '"Acme Foods"' and seen["timespan"] == "30d"
    assert out == [{"title": "Acme Foods wins award", "url": "https://n.example/x", "source": "n.example", "date": "2026-11-10"}]


def test_the_real_domain_helper_drops_the_brand_itself_and_junk(monkeypatch):
    import importlib
    import dnstwist
    real = importlib.reload(brand_check)
    monkeypatch.setattr(dnstwist, "run", lambda **kw: [{"domain": "acmefoods.com"}, {"domain": "acmef00ds.com"}, {"domain": "acmef00ds.com"},
                                                       {"domain": "bad domain"}, {"domain": "ACMEFOODS.NET."}])
    assert real.find_domains("acmefoods.com") == ["acmef00ds.com", "acmefoods.net"]


def test_rating_falls_back_to_watch_without_ai():
    import importlib
    real = importlib.reload(brand_check)
    assert real.rate_news("Acme", "acme.com", [{"title": "a"}, {"title": "b"}]) == ["WATCH", "WATCH"]


def test_the_page_never_claims_an_email_that_was_not_sent(client, db, monkeypatch):
    _on(db)
    from app.services import mailer
    monkeypatch.setattr(mailer, "send_email", lambda *a, **k: False)          # no email service, or it failed
    body = _start(client).json()
    brand_check.process_queue(db)
    view = client.get(f"/api/public/brand-check/{body['id']}", params={"t": body["token"]}).json()
    assert view["status"] == "done" and view["emailed"] is False


def test_the_email_is_sent_before_the_page_sees_done(client, db, monkeypatch):
    _on(db)
    body = _start(client).json()
    seen = {}
    from app.services import mailer

    def send(*a, **k):
        seen["status"] = db.get(BrandCheck, body["id"]).status
        return True
    monkeypatch.setattr(mailer, "send_email", send)
    brand_check.process_queue(db)
    assert seen["status"] == "running"
