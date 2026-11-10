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
    assert r.status_code == 200 and r.json() == {"enabled": True, "project": PROJECT, "custom": []}


def test_the_public_answer_carries_nothing_else(client, adm):
    client.put("/api/admin/badges", headers=adm, json={"enabled": False, "project": OTHER})
    assert set(client.get("/api/public/badges-config").json()) == {"enabled", "project", "custom"}


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
    assert client.get("/api/public/badges-config").json() == {"enabled": True, "project": PROJECT, "custom": []}


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


# ================================================================ other directory badges (added by hand in Super Admin)
SAASHUB = {"name": "SaaSHub", "href": "https://www.saashub.com/brandslens", "image": "https://cdn.saashub.test/badge-light.png", "alt": "", "width": 120, "height": 54, "enabled": True}
ALTTO = {"name": "AlternativeTo", "href": "https://alternativeto.net/software/brandslens/", "image": "https://alternativeto.net/badge.svg", "enabled": True}


def save(client, adm, *items):
    return client.put("/api/admin/badges/custom", headers=adm, json={"items": list(items)})


def test_there_are_no_other_badges_until_you_add_some(client, adm):
    assert client.get("/api/admin/badges", headers=adm).json()["custom"] == []
    assert client.get("/api/public/badges-config").json()["custom"] == []


def test_a_badge_is_saved_cleaned_and_given_a_stable_id(client, adm):
    r = save(client, adm, SAASHUB, ALTTO)
    assert r.status_code == 200
    a, b = r.json()["custom"]
    assert a["name"] == "SaaSHub" and a["alt"] == "Listed on SaaSHub" and a["width"] == 120 and a["height"] == 54 and a["enabled"] is True
    assert b["width"] is None and b["height"] is None and b["alt"] == "Listed on AlternativeTo"
    assert len(a["id"]) == 12 and a["id"] != b["id"]
    again = save(client, adm, *r.json()["custom"]).json()["custom"]                 # saving what came back changes nothing, ids included
    assert [x["id"] for x in again] == [a["id"], b["id"]]


def test_the_public_answer_shows_only_enabled_badges_with_only_the_fields_the_page_needs(client, adm):
    save(client, adm, SAASHUB, {**ALTTO, "enabled": False})
    shown = client.get("/api/public/badges-config").json()["custom"]
    assert [x["name"] for x in shown] == ["SaaSHub"]
    assert set(shown[0]) == {"name", "href", "image", "alt", "width", "height"}


def test_the_order_you_save_is_the_order_the_page_gets(client, adm):
    save(client, adm, ALTTO, SAASHUB)
    assert [x["name"] for x in client.get("/api/public/badges-config").json()["custom"]] == ["AlternativeTo", "SaaSHub"]
    save(client, adm, SAASHUB, ALTTO)
    assert [x["name"] for x in client.get("/api/public/badges-config").json()["custom"]] == ["SaaSHub", "AlternativeTo"]


def test_an_empty_save_removes_them_all(client, adm):
    save(client, adm, SAASHUB)
    assert save(client, adm).json()["custom"] == [] and client.get("/api/public/badges-config").json()["custom"] == []


@pytest.mark.parametrize("field,bad", [
    ("href", "http://www.saashub.com/brandslens"), ("href", "javascript:alert(1)"), ("href", "//saashub.com/x"), ("href", "/relative"), ("href", "https://saashub/x"),
    ("href", "https://user:pw@saashub.com/x"), ("href", "https://saas hub.com/x"), ("href", 'https://saashub.com/x"onmouseover="alert(1)'), ("href", "https://saashub.com/<x>"),
    ("href", "data:text/html,<script>alert(1)</script>"), ("href", "https://saashub.com/" + "a" * 500), ("href", ""),
    ("image", "http://cdn.test/badge.png"), ("image", "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="), ("image", "javascript:alert(1)"), ("image", ""), ("image", "https://cdn.test/a b.png"),
])
def test_an_address_that_is_not_a_plain_https_address_is_refused_and_nothing_is_saved(client, adm, field, bad):
    r = save(client, adm, {**SAASHUB, field: bad})
    assert r.status_code == 422 and "SaaSHub" in r.json()["detail"]
    assert client.get("/api/public/badges-config").json()["custom"] == []


@pytest.mark.parametrize("patch,needle", [({"name": ""}, "name"), ({"name": "x" * 61}, "name"), ({"width": 5}, "width and height"), ({"height": 9000}, "width and height"), ({"alt": "x" * 121}, "alt text")])
def test_a_bad_name_size_or_alt_is_refused_with_a_reason(client, adm, patch, needle):
    r = save(client, adm, {**SAASHUB, **patch})
    assert r.status_code == 422 and needle in r.json()["detail"]


def test_one_bad_badge_refuses_the_whole_save_so_a_good_list_is_never_half_replaced(client, adm):
    save(client, adm, SAASHUB)
    assert save(client, adm, ALTTO, {**SAASHUB, "href": "ftp://x.test"}).status_code == 422
    assert [x["name"] for x in client.get("/api/public/badges-config").json()["custom"]] == ["SaaSHub"]


def test_there_is_a_limit_on_how_many_can_be_added(client, adm):
    many = [{**SAASHUB, "name": f"Dir {i}"} for i in range(25)]
    r = save(client, adm, *many)
    assert r.status_code == 422 and "24" in r.json()["detail"]
    assert save(client, adm, *many[:24]).status_code == 200


def test_two_badges_sent_with_the_same_id_are_given_different_ones(client, adm):
    ids = [x["id"] for x in save(client, adm, {**SAASHUB, "id": "aaaaaaaaaaaa"}, {**ALTTO, "id": "aaaaaaaaaaaa"}).json()["custom"]]
    assert len(set(ids)) == 2


def test_a_bad_value_somehow_stored_never_reaches_the_public_page(client, db):
    from app.services.settings import set_setting
    set_setting(db, "badges:custom", [{"name": "Evil", "href": "javascript:alert(1)", "image": "https://x.test/a.png"}, "junk", 5,
                                      {"name": "Fine", "href": "https://fine.test/p", "image": "https://fine.test/b.png", "enabled": True}], "test")
    assert [x["name"] for x in client.get("/api/public/badges-config").json()["custom"]] == ["Fine"]
    set_setting(db, "badges:custom", "not a list", "test")
    assert client.get("/api/public/badges-config").json()["custom"] == []


def test_only_an_admin_can_change_the_other_badges(client, db):
    _, token, _ = make_customer(db)
    assert client.put("/api/admin/badges/custom", headers=bearer(token), json={"items": [SAASHUB]}).status_code == 403
    assert client.put("/api/admin/badges/custom", json={"items": [SAASHUB]}).status_code in (401, 403)
    assert client.get("/api/public/badges-config").json()["custom"] == []


def test_the_main_switch_and_the_project_are_not_disturbed_by_saving_badges(client, adm):
    client.put("/api/admin/badges", headers=adm, json={"enabled": False, "project": OTHER})
    save(client, adm, SAASHUB)
    d = client.get("/api/public/badges-config").json()
    assert d["enabled"] is False and d["project"] == OTHER and len(d["custom"]) == 1
    client.put("/api/admin/badges", headers=adm, json={"enabled": True, "project": ""})          # changing the switch does not touch the list
    assert len(client.get("/api/public/badges-config").json()["custom"]) == 1


def test_the_install_check_reports_each_badge_by_name(client, adm, monkeypatch):
    save(client, adm, SAASHUB, ALTTO, {**{"name": "Hidden", "href": "https://hidden.test/p", "image": "https://hidden.test/b.png"}, "enabled": False})
    page = f'<div data-sbm-ref="{PROJECT}"></div><a href="https://www.saashub.com/brandslens"><img src="x"/></a>'
    fake_site(monkeypatch, page)
    r = client.post("/api/admin/badges/check", headers=adm).json()
    assert r["custom"] == [{"name": "SaaSHub", "found": True}, {"name": "AlternativeTo", "found": False}]       # a switched-off badge is not looked for


def test_the_install_check_finds_a_link_the_page_wrote_with_html_escaping(client, adm, monkeypatch):
    save(client, adm, {**SAASHUB, "href": "https://www.saashub.com/p?a=1&b=2"})
    fake_site(monkeypatch, '<a href="https://www.saashub.com/p?a=1&amp;b=2">x</a>')
    assert client.post("/api/admin/badges/check", headers=adm).json()["custom"] == [{"name": "SaaSHub", "found": True}]


def test_the_install_check_lists_no_other_badges_when_the_page_cannot_be_read(client, adm, monkeypatch):
    save(client, adm, SAASHUB)
    fake_site(monkeypatch, status=503)
    assert client.post("/api/admin/badges/check", headers=adm).json()["custom"] == []
