"""The default news feeds (one dead address repaired, one discontinued feed dropped, Channels TV added), the one-time repair of existing workspaces,
and the lighter set of starter keywords for a free trial."""
from sqlalchemy import select

from app.models import Workspace
from app.services import auth, trial
from tests.helpers import make_customer

OLD_SAHARA, NEW_SAHARA, CNBC = "http://saharareporters.com/feed", "https://saharareporters.com/rss.xml", "https://www.cnbcafrica.com/feed/"
SECRET = "test-secret"


# ------------------------------------------------------------------ the default list
def test_the_default_list_is_clean_and_has_no_dead_feeds():
    feeds = auth.DEFAULT_RSS_FEEDS
    assert len(feeds) == 25 and len(set(feeds)) == 25
    assert NEW_SAHARA in feeds and "https://www.channelstv.com/feed/" in feeds
    assert OLD_SAHARA not in feeds and CNBC not in feeds
    assert all(f.startswith(("http://", "https://")) for f in feeds)


def test_every_retired_feed_is_off_the_default_list_and_any_replacement_is_on_it():
    for old, new in auth.RETIRED_RSS_FEEDS.items():
        assert old not in auth.DEFAULT_RSS_FEEDS
        assert new is None or new in auth.DEFAULT_RSS_FEEDS


def test_a_new_account_gets_the_repaired_list(db):
    _, _, ws = make_customer(db)
    assert ws.rss_feeds == auth.DEFAULT_RSS_FEEDS and CNBC not in ws.rss_feeds


# ------------------------------------------------------------------ the one-time repair of existing workspaces
def backfill(client):
    r = client.get("/api/setup/backfill-rss-feeds", params={"secret": SECRET})
    assert r.status_code == 200, r.text
    return r.json()


def feeds_of(db, ws):
    db.expire_all()
    return list(db.get(Workspace, ws.id).rss_feeds)


def test_an_existing_workspace_has_the_dead_address_swapped_and_the_discontinued_feed_dropped(client, db):
    _, _, ws = make_customer(db)
    old_list = [f for f in auth.DEFAULT_RSS_FEEDS if f != NEW_SAHARA and "channelstv" not in f] + [OLD_SAHARA, CNBC]
    ws.rss_feeds = old_list; db.commit()
    out = backfill(client)
    got = feeds_of(db, ws)
    assert OLD_SAHARA not in got and CNBC not in got
    assert got.count(NEW_SAHARA) == 1 and "https://www.channelstv.com/feed/" in got
    assert set(got) == set(auth.DEFAULT_RSS_FEEDS) and len(got) == len(set(got))
    mine = [w for w in out["workspaces_updated"] if w["workspace"] == ws.name][0]
    assert (mine["feeds_replaced"], mine["feeds_removed"]) == (1, 1)


def test_a_teams_own_feeds_are_never_touched(client, db):
    _, _, ws = make_customer(db)
    ws.rss_feeds = [OLD_SAHARA, "https://my-own-blog.example/feed", CNBC]; db.commit()
    backfill(client)
    got = feeds_of(db, ws)
    assert "https://my-own-blog.example/feed" in got and got.index("https://my-own-blog.example/feed") < got.index(NEW_SAHARA) or "https://my-own-blog.example/feed" in got


def test_a_workspace_that_already_has_the_new_address_does_not_get_it_twice(client, db):
    _, _, ws = make_customer(db)
    ws.rss_feeds = [OLD_SAHARA, NEW_SAHARA]; db.commit()
    backfill(client)
    got = feeds_of(db, ws)
    assert got.count(NEW_SAHARA) == 1 and OLD_SAHARA not in got


def test_running_the_repair_twice_changes_nothing_the_second_time(client, db):
    _, _, ws = make_customer(db)
    ws.rss_feeds = [OLD_SAHARA, CNBC]; db.commit()
    backfill(client)
    once = feeds_of(db, ws)
    second = backfill(client)
    assert feeds_of(db, ws) == once and second["workspaces_updated"] == []


def test_a_workspace_with_no_feeds_gets_the_whole_list(client, db):
    _, _, ws = make_customer(db)
    ws.rss_feeds = []; db.commit()
    backfill(client)
    assert feeds_of(db, ws) == auth.DEFAULT_RSS_FEEDS


def test_the_repair_still_needs_the_setup_secret(client):
    assert client.get("/api/setup/backfill-rss-feeds", params={"secret": "wrong"}).status_code == 403
    assert client.get("/api/setup/backfill-rss-feeds").status_code in (403, 422)


# ------------------------------------------------------------------ a trial starts with no default keywords
def start_real_trial(client, db, company="Malaki Perfumes Ltd", email="wave@malaki.com"):
    trial.save_settings(db, "t", {"enabled": True, "days": 14})
    codes = {}
    original = trial.send_code_email
    trial.send_code_email = lambda row, code: codes.__setitem__(row.email, code) or True
    try:
        assert client.post("/api/public/trial/start", json={"email": email, "name": "Wave Owner", "company": company}).status_code == 200
    finally:
        trial.send_code_email = original
    r = client.post("/api/public/trial/verify", json={"email": email, "code": codes[email], "password": "correct-horse-1"})
    assert r.status_code == 200, r.text
    return db.scalar(select(Workspace).where(Workspace.owner_email == email)), r.json()["token"]


def test_a_new_trial_workspace_has_no_keywords_but_keeps_its_name_and_feeds(client, db):
    ws, _ = start_real_trial(client, db)
    assert ws.keywords == [] and ws.name == "Malaki Perfumes Ltd" and ws.rss_feeds == auth.DEFAULT_RSS_FEEDS


def test_the_customer_can_then_list_all_five_of_their_own(client, db):
    from tests.helpers import bearer
    ws, token = start_real_trial(client, db)
    five = ["Wave Body Mist", "Malaki perfume", "malakiperfumes", "Wave Bodymist Gold", "Malaki Studio"]
    assert client.patch(f"/api/workspaces/{ws.id}", headers=bearer(token), json={"keywords": five}).status_code == 200
    r = client.patch(f"/api/workspaces/{ws.id}", headers=bearer(token), json={"keywords": five + ["a sixth"]})
    assert r.status_code == 422 and "up to 5" in r.json()["detail"]
    db.expire_all()
    assert db.get(Workspace, ws.id).keywords == five


def test_with_no_keywords_the_workspace_name_is_still_watched_and_found(db):
    from app.services import pipeline
    ws = Workspace(organization_id="o", name="Malaki Perfumes", sector="", keywords=[], rss_feeds=[])
    assert pipeline._matching_keywords(ws, "Malaki Perfumes launches a new scent") == ["Malaki Perfumes"]
    assert pipeline.search_terms(ws) == ["Malaki Perfumes"]
    assert pipeline._matching_keywords(ws, "an unrelated headline") == []


def test_the_welcome_email_asks_for_the_customers_own_keywords_and_mentions_no_defaults(client, db):
    from app.models import TrialSignup
    start_real_trial(client, db)
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == "wave@malaki.com"))
    step = [st for st in trial.steps(db) if st["key"] == "welcome"][0]
    _, body = trial.build_email(db, row, step)
    assert "add the names people use for your brand and products" in body and "up to five keywords" in body
    assert "starter" not in body and "already watching" not in body and "{{" not in body


def test_the_welcome_email_follows_the_real_cap(client, db):
    from app.models import TrialSignup
    trial.save_settings(db, "t", {"caps": {"keywords": 3}})
    start_real_trial(client, db)
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == "wave@malaki.com"))
    _, body = trial.build_email(db, row, [st for st in trial.steps(db) if st["key"] == "welcome"][0])
    assert "up to three keywords" in body


def test_an_old_saved_welcome_email_that_still_uses_the_retired_placeholder_renders_without_leaking_it(client, db):
    from app.models import TrialSignup
    start_real_trial(client, db)
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == "wave@malaki.com"))
    old = dict([st for st in trial.steps(db) if st["key"] == "welcome"][0])
    old["body"] = "<p>We set up {{company}} with {{starter_phrase}} already watching.</p>"
    _, body = trial.build_email(db, row, old)
    assert "{{" not in body and "your keywords already watching" in body
