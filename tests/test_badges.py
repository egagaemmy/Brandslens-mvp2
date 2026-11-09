"""The directory-badge strip on the marketing homepage: a public switch the website reads, Super Admin controls, and an install check."""
import httpx
import pytest

from app import main
from tests.helpers import bearer, make_admin, make_customer

PROJECT = main.DEFAULT_BADGES_PROJECT
OTHER = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def adm(db):
    _, token = make_admin(db)
    return bearer(token)


class Page:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def fake_site(monkeypatch, text="<html></html>", status=200, boom=False):
    seen = []
    def get(url, **kw):
        seen.append((url, kw))
        if boom:
            raise httpx.ConnectError("down")
        return Page(text, status)
    monkeypatch.setattr(main.httpx, "get", get)
    return seen


# ---------------------------------------------------------------- the public switch
def test_the_strip_is_shown_by_default_with_the_default_project(client):
    r = client.get("/api/public/badges-config")
    assert r.status_code == 200 and r.json() == {"enabled": True, "project": PROJECT}


def test_the_public_answer_carries_nothing_else(client, adm):
    client.put("/api/admin/badges", headers=adm, json={"enabled": False, "project": OTHER})
    assert set(client.get("/api/public/badges-config").json()) == {"enabled", "project"}


def test_switching_it_off_and_on_is_seen_by_the_public_endpoint(client, adm):
    assert client.put("/api/admin/badges", headers=adm, json={"enabled": False, "project": ""}).json()["enabled"] is False
    assert client.get("/api/public/badges-config").json()["enabled"] is False
    client.put("/api/admin/badges", headers=adm, json={"enabled": True, "project": ""})
    assert client.get("/api/public/badges-config").json()["enabled"] is True


def test_another_project_can_be_chosen_and_a_blank_returns_to_the_default(client, adm):
    client.put("/api/admin/badges", headers=adm, json={"enabled": True, "project": OTHER.upper()})
    assert client.get("/api/public/badges-config").json()["project"] == OTHER           # stored in lower case
    client.put("/api/admin/badges", headers=adm, json={"enabled": True, "project": "  "})
    assert client.get("/api/public/badges-config").json()["project"] == PROJECT


@pytest.mark.parametrize("bad", ["abc", "not-a-project", OTHER + "x", "../etc/passwd", "javascript:alert(1)", f'{OTHER}"><script>', "11111111222233334444555555555555"])
def test_a_project_id_that_is_not_one_is_refused_and_nothing_changes(client, adm, bad):
    r = client.put("/api/admin/badges", headers=adm, json={"enabled": False, "project": bad})
    assert r.status_code == 422 and "project ID" in r.json()["detail"]
    assert client.get("/api/public/badges-config").json() == {"enabled": True, "project": PROJECT}


def test_a_bad_value_somehow_stored_never_reaches_the_website(client, db):
    from app.services.settings import set_setting
    set_setting(db, "badges:project", '"><script>alert(1)</script>', "test")
    assert client.get("/api/public/badges-config").json()["project"] == PROJECT


# ---------------------------------------------------------------- only an admin may change or check it
def test_only_an_admin_can_read_change_or_check_it(client, db):
    _, token, _ = make_customer(db)
    customer = bearer(token)
    for method, path, body in (("get", "/api/admin/badges", None), ("put", "/api/admin/badges", {"enabled": False, "project": ""}), ("post", "/api/admin/badges/check", None)):
        kw = {"json": body} if body is not None else {}
        assert getattr(client, method)(path, headers=customer, **kw).status_code == 403
        assert getattr(client, method)(path, **kw).status_code in (401, 403)
    assert client.get("/api/public/badges-config").json()["enabled"] is True


def test_the_admin_view_shows_the_effective_project_the_default_and_the_site(client, adm):
    d = client.get("/api/admin/badges", headers=adm).json()
    assert d["enabled"] is True and d["project"] == PROJECT and d["default_project"] == PROJECT and d["site_url"].startswith("https://")


# ---------------------------------------------------------------- the install check (the same check Submitator runs)
def test_the_check_passes_when_the_marker_is_in_the_html(client, adm, monkeypatch):
    seen = fake_site(monkeypatch, f'<footer><div data-sbm-ref="{PROJECT}"></div></footer>')
    r = client.post("/api/admin/badges/check", headers=adm).json()
    assert r["ok"] is True and r["found"] == 1 and r["status"] == 200 and "Verify Badge Handling" in r["message"]
    url, kw = seen[0]
    assert url.endswith("/") and kw["follow_redirects"] is True and kw["timeout"] <= 20


def test_the_check_also_accepts_the_marker_without_quotes_and_counts_every_one(client, adm, monkeypatch):
    fake_site(monkeypatch, f"<a data-sbm-ref={PROJECT}></a><a data-sbm-ref=\"{PROJECT}\"></a>")
    assert client.post("/api/admin/badges/check", headers=adm).json()["found"] == 2


def test_the_check_fails_with_a_clear_next_step_when_the_badges_are_not_in_the_page(client, adm, monkeypatch):
    fake_site(monkeypatch, "<html><footer>no badges</footer></html>")
    r = client.post("/api/admin/badges/check", headers=adm).json()
    assert r["ok"] is False and r["found"] == 0 and "deployed once" in r["message"] and "60 seconds" in r["message"]


def test_the_check_looks_for_the_chosen_project_not_another(client, adm, monkeypatch):
    client.put("/api/admin/badges", headers=adm, json={"enabled": True, "project": OTHER})
    fake_site(monkeypatch, f'<div data-sbm-ref="{PROJECT}"></div>')                  # a marker for a different project does not count
    assert client.post("/api/admin/badges/check", headers=adm).json()["ok"] is False
    fake_site(monkeypatch, f'<div data-sbm-ref="{OTHER}"></div>')
    assert client.post("/api/admin/badges/check", headers=adm).json()["ok"] is True


def test_the_check_says_so_when_the_strip_is_switched_off(client, adm, monkeypatch):
    client.put("/api/admin/badges", headers=adm, json={"enabled": False, "project": ""})
    fake_site(monkeypatch, "<html></html>")
    assert "switched off" in client.post("/api/admin/badges/check", headers=adm).json()["message"]


def test_the_check_reports_a_website_that_errors_or_cannot_be_reached(client, adm, monkeypatch):
    fake_site(monkeypatch, status=503)
    r = client.post("/api/admin/badges/check", headers=adm).json()
    assert r["ok"] is False and r["status"] == 503 and "503" in r["message"]
    fake_site(monkeypatch, boom=True)
    r = client.post("/api/admin/badges/check", headers=adm).json()
    assert r["ok"] is False and r["status"] is None and "could not reach" in r["message"]


def test_the_check_only_ever_fetches_the_configured_site_never_an_address_from_the_request(client, adm, monkeypatch):
    seen = fake_site(monkeypatch, "<html></html>")
    client.post("/api/admin/badges/check?url=http://169.254.169.254/", headers=adm, json={"url": "http://evil.test/"})
    assert [u for u, _ in seen] == [main.MARKETING_URL.rstrip("/") + "/"]
