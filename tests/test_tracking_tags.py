"""Ad and analytics tags managed from Super Admin, the public list the websites read, and the UTM link builder."""
import json

import pytest

from app.models import TrackingTag
from app.services import tracking
from tests.helpers import bearer, make_admin, make_customer


@pytest.fixture
def admin(client, db):
    make_admin(db)
    return bearer(client.post("/api/auth/login", json={"email": "admin@brandslens.test", "password": "admin-pass-123"}).json()["token"])


def add(client, admin, provider="meta_pixel", tag_id="123456789012345", **kw):
    return client.post("/api/admin/tracking/tags", headers=admin, json={"provider": provider, "tag_id": tag_id, "scope": "marketing", **kw})


def public(client, scope="marketing"):
    return client.get(f"/api/public/tracking-config?scope={scope}")


# ---------------------------------------------------------------- who may change what visitors' browsers load
def test_only_the_brandslens_team_can_manage_tags_and_links(client, db):
    _, token, _ = make_customer(db)
    calls = (("GET", "/api/admin/tracking/tags"), ("POST", "/api/admin/tracking/tags"), ("PATCH", "/api/admin/tracking/tags/x"), ("DELETE", "/api/admin/tracking/tags/x"),
             ("GET", "/api/admin/utm-links"), ("POST", "/api/admin/utm-links"), ("DELETE", "/api/admin/utm-links/x"))
    for method, path in calls:
        assert client.request(method, path, headers=bearer(token), json={"provider": "meta_pixel", "tag_id": "123456789012345", "scope": "marketing", "destination": "/launch", "utm_source": "x"}).status_code == 403
        assert client.request(method, path, json={}).status_code == 401


# ---------------------------------------------------------------- adding tags
@pytest.mark.parametrize("provider", list(tracking.PROVIDERS))
def test_every_supported_platform_accepts_its_own_id_format(client, admin, provider):
    r = add(client, admin, provider, tracking.PROVIDERS[provider]["example"])
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == provider and r.json()["enabled"] is True


@pytest.mark.parametrize("provider,tag_id,expected", [("clarity", "abcd1234ef", "analytics"), ("google_tag", "G-ABC123XYZ9", "analytics"), ("google_tag", "AW-123456789", "marketing"),
                                                      ("google_tag", "GTM-ABC1234", "marketing"), ("meta_pixel", "123456789012345", "marketing"), ("tiktok_pixel", "C1ABCDEFGHIJKLMNOPQR", "marketing")])
def test_the_consent_category_is_decided_by_the_server_never_by_the_person_adding_the_tag(client, admin, provider, tag_id, expected):
    r = client.post("/api/admin/tracking/tags", headers=admin, json={"provider": provider, "tag_id": tag_id, "scope": "marketing", "category": "analytics"})
    assert r.status_code == 200 and r.json()["category"] == expected


def test_ids_are_tidied_into_the_platforms_own_format(client, admin):
    assert add(client, admin, "google_tag", "  g-abc123xyz9 ").json()["tag_id"] == "G-ABC123XYZ9"
    assert add(client, admin, "x_pixel", "O1ABC").json()["tag_id"] == "o1abc"
    assert add(client, admin, "meta_pixel", " 123456789012345 ").json()["tag_id"] == "123456789012345"


@pytest.mark.parametrize("provider,bad", [("meta_pixel", '");alert(1);//'), ("meta_pixel", "<script>alert(1)</script>"), ("meta_pixel", "12345"), ("google_tag", "UA-12345-1"),
                                          ("google_tag", "G-abc'); fetch('//evil.example') //"), ("linkedin_insight", "abc"), ("x_pixel", "o1 abc"), ("tiktok_pixel", "short"),
                                          ("snap_pixel", "not-a-uuid"), ("clarity", ""), ("nonsense", "123456789012345")])
def test_anything_that_is_not_a_real_id_is_refused_and_nothing_is_saved(client, admin, provider, bad):
    r = add(client, admin, provider, bad)
    assert r.status_code == 422
    assert client.get("/api/admin/tracking/tags", headers=admin).json()["tags"] == []


def test_the_error_tells_the_person_what_a_good_id_looks_like(client, admin):
    msg = add(client, admin, "meta_pixel", "abc").json()["detail"]
    assert "123456789012345" in msg and "Events Manager" in msg


def test_the_same_tag_cannot_be_added_twice(client, admin):
    assert add(client, admin).status_code == 200
    assert add(client, admin).status_code == 409


def test_a_tag_must_say_where_it_runs(client, admin):
    assert client.post("/api/admin/tracking/tags", headers=admin, json={"provider": "meta_pixel", "tag_id": "123456789012345", "scope": "everywhere"}).status_code == 422


# ---------------------------------------------------------------- changing and removing
def test_a_tag_can_be_switched_off_on_again_relabelled_and_given_a_new_id(client, admin):
    tag = add(client, admin).json()
    url = f"/api/admin/tracking/tags/{tag['id']}"
    assert client.patch(url, headers=admin, json={"enabled": False}).json()["enabled"] is False
    assert client.patch(url, headers=admin, json={"enabled": True, "label": "Launch pixel"}).json()["label"] == "Launch pixel"
    assert client.patch(url, headers=admin, json={"tag_id": "999999999999999"}).json()["tag_id"] == "999999999999999"
    assert client.patch(url, headers=admin, json={"tag_id": "oops"}).status_code == 422
    assert client.patch("/api/admin/tracking/tags/missing", headers=admin, json={"enabled": False}).status_code == 404


def test_conversion_ids_are_checked_too(client, admin):
    tag = add(client, admin, "linkedin_insight", "1234567").json()
    url = f"/api/admin/tracking/tags/{tag['id']}"
    assert client.patch(url, headers=admin, json={"events": {"lead": "7654321"}}).json()["events"] == {"lead": "7654321"}
    assert client.patch(url, headers=admin, json={"events": {"lead": "bad id"}}).status_code == 422
    assert client.patch(url, headers=admin, json={"events": {"signup": "7654321"}}).status_code == 422
    meta = add(client, admin).json()
    assert client.patch(f"/api/admin/tracking/tags/{meta['id']}", headers=admin, json={"events": {"lead": "1234567"}}).status_code == 422


def test_a_tag_can_be_removed(client, admin):
    tag = add(client, admin).json()
    assert client.delete(f"/api/admin/tracking/tags/{tag['id']}", headers=admin).json() == {"deleted": True}
    assert client.delete(f"/api/admin/tracking/tags/{tag['id']}", headers=admin).status_code == 404
    assert client.get("/api/admin/tracking/tags", headers=admin).json()["tags"] == []


def test_the_screen_gets_every_platform_with_an_example_and_where_to_find_the_id(client, admin):
    providers = client.get("/api/admin/tracking/tags", headers=admin).json()["providers"]
    assert {p["provider"] for p in providers} == set(tracking.PROVIDERS)
    assert all(p["example"] and p["where"] and p["name"] for p in providers)


# ---------------------------------------------------------------- what the websites are told
def test_the_marketing_site_is_told_only_about_enabled_tags_meant_for_it(client, admin):
    add(client, admin, "meta_pixel", "123456789012345")
    off = add(client, admin, "linkedin_insight", "1234567").json()
    client.patch(f"/api/admin/tracking/tags/{off['id']}", headers=admin, json={"enabled": False})
    client.post("/api/admin/tracking/tags", headers=admin, json={"provider": "tiktok_pixel", "tag_id": "C1ABCDEFGHIJKLMNOPQR", "scope": "app"})
    client.post("/api/admin/tracking/tags", headers=admin, json={"provider": "google_tag", "tag_id": "G-ABC123XYZ9", "scope": "both"})
    marketing = public(client).json()
    assert [t["provider"] for t in marketing["tags"]] == ["meta_pixel", "google_tag"]
    assert marketing["consent_required"] is True
    assert [t["provider"] for t in public(client, "app").json()["tags"]] == ["tiktok_pixel", "google_tag"]


def test_the_public_list_carries_no_labels_no_emails_no_timestamps(client, admin):
    add(client, admin, label="Secret internal name")
    body = public(client).json()
    assert set(body) == {"consent_required", "tags"} and set(body["tags"][0]) == {"provider", "id", "category", "events"}
    assert "Secret" not in json.dumps(body) and "admin@brandslens.test" not in json.dumps(body)


def test_the_public_list_is_cacheable_and_only_answers_for_real_sites(client, admin):
    r = public(client)
    assert r.status_code == 200 and "max-age=60" in r.headers["cache-control"]
    assert public(client, "elsewhere").status_code == 422


def test_a_malformed_id_edited_in_by_hand_is_never_published(client, admin, db):
    add(client, admin)
    db.add(TrackingTag(provider="meta_pixel", tag_id='");alert(1);//', scope="marketing", enabled=True))
    db.add(TrackingTag(provider="ghost_platform", tag_id="123456789012345", scope="marketing", enabled=True))
    db.commit()
    published = public(client).json()["tags"]
    assert [t["id"] for t in published] == ["123456789012345"]


def test_nothing_is_published_when_nothing_is_enabled(client, admin):
    assert public(client).json() == {"consent_required": True, "tags": []}


# ---------------------------------------------------------------- UTM links
def utm(client, admin, **kw):
    body = {"label": "Launch post", "destination": "/launch", "utm_source": "producthunt", "utm_medium": "launch", "utm_campaign": "official_launch", **kw}
    return client.post("/api/admin/utm-links", headers=admin, json=body)


def test_a_campaign_link_is_built_in_the_one_agreed_format(client, admin):
    r = utm(client, admin, utm_content="banner_a", utm_term="media_monitoring")
    assert r.status_code == 200
    assert r.json()["url"] == "https://www.brandslens.com/launch?utm_source=producthunt&utm_medium=launch&utm_campaign=official_launch&utm_term=media_monitoring&utm_content=banner_a"


def test_campaign_tags_are_tidied_so_the_team_cannot_make_inconsistent_links(client, admin):
    r = utm(client, admin, utm_source="Product Hunt!", utm_campaign="Black Friday 2026")
    assert r.json()["utm_source"] == "producthunt" and r.json()["utm_campaign"] == "blackfriday2026"


def test_a_link_may_point_at_any_brandslens_site_but_nowhere_else(client, admin):
    assert utm(client, admin, destination="https://app.brandslens.com/signup").json()["url"].startswith("https://app.brandslens.com/signup?utm_source=")
    for bad in ("https://evil.example/launch", "https://brandslens.com.evil.example/x", "http://www.brandslens.com/launch", "/launch?x=1", "//evil.example", "javascript:alert(1)", "/a b", ""):
        assert utm(client, admin, destination=bad).status_code == 422, bad


def test_a_link_needs_a_source(client, admin):
    assert utm(client, admin, utm_source="  !! ").status_code == 422


def test_saved_links_are_listed_newest_first_and_can_be_deleted(client, admin):
    first = utm(client, admin, label="first").json(); second = utm(client, admin, label="second", utm_source="hackernews").json()
    labels = [x["label"] for x in client.get("/api/admin/utm-links", headers=admin).json()["links"]]
    assert labels == ["second", "first"]
    assert client.delete(f"/api/admin/utm-links/{first['id']}", headers=admin).json() == {"deleted": True}
    assert client.delete(f"/api/admin/utm-links/{first['id']}", headers=admin).status_code == 404
    assert [x["label"] for x in client.get("/api/admin/utm-links", headers=admin).json()["links"]] == ["second"]
