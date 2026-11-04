"""Five fixes after a free-trial customer saw no mentions: spellings are matched loosely, the news index's own match is trusted, the screen shows a
trial's real limits, and the customer can see that BrandsLens is looking (without ever hearing about a source that is not connected)."""
import random
import re
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import worker
from app.collectors import hackernews_collector, news_collector, reddit_collector, x_collector, youtube_collector
from app.models import Incident, Organization, ScanRun, Workspace, now_utc
from app.services import matching, pipeline, scan_health, trial
from tests.helpers import bearer, make_customer

PASSWORD = "correct-horse-1"


def ws_with(name="Malaki", keywords=(), **kw):
    return Workspace(organization_id="o", name=name, sector="FMCG", keywords=list(keywords), rss_feeds=[], **kw)


# ================================================================ 2. spellings are matched loosely
@pytest.mark.parametrize("text", ["Wave Body Mist", "wave body mist", "WAVE BODY MIST", "Wave Bodymist", "wave body-mist", "Wave_Body_Mist", "wave  body  mist", "WaveBodyMist",
                                  "Just bought the wave bodymist, love it", "#WaveBodyMist is trending", "wave-body-mist.com"])
def test_every_way_of_writing_the_name_is_caught(text):
    assert matching.contains(text.lower(), "Wave Body Mist")


@pytest.mark.parametrize("text", ["Wave Body", "a wave of body mist", "wave. body mist", "waves body mists", "Body Mist by another brand", "wave bodies of mist", "wave, body, mist"])
def test_things_that_are_not_the_name_are_not_caught(text):
    assert not matching.contains(text.lower(), "Wave Body Mist")


def test_a_short_name_written_as_two_words_is_not_glued_into_an_ordinary_word():
    assert matching.contains("sun day sale".lower(), "Sun Day") and matching.contains("sun-day sale", "Sun Day")
    assert not matching.contains("see you on sunday", "Sun Day") and not matching.contains("payday loans", "Pay Day")


def test_a_longer_name_may_be_written_as_one_word():
    assert matching.contains("i love facebook", "Face Book") and matching.contains("airtelnigeria is down", "Airtel Nigeria")


def test_the_old_exact_rule_still_matches_everything_it_did():
    """Never fewer matches than before: plurals, apostrophes, punctuation, domains and one-word keywords are all as they were."""
    assert matching.contains("loving malaki perfumes", "malaki perfume")
    assert matching.contains("mcdonald's is open", "McDonald's") and matching.contains("visit malakiperfumes.com today", "malakiperfumes.com")
    assert matching.contains("a dangotes story", "Dangote") and matching.contains("4g-lte plans", "4G-LTE")
    assert matching.contains("#brand!", "#brand!")


def test_it_never_matches_less_than_the_old_rule_on_a_large_random_sample():
    rng = random.Random(7)
    words = ["wave", "body", "mist", "malaki", "perfume", "sun", "day", "air", "tel", "pay", "face", "book", "naija", "bank", "go", "fast", "m", "pesa", "-", "_", ".", ",", "'", "#", "!"]
    for _ in range(4000):
        kw = " ".join(rng.choice(words) for _ in range(rng.randint(1, 3))).strip()
        text = (" " + rng.choice(["", "-", "_", "  ", "."]) + " ").join(rng.choice(words) for _ in range(rng.randint(2, 6)))
        if kw and kw.lower() in text.lower():
            assert matching.contains(text.lower(), kw), (kw, text)


def test_a_blank_keyword_matches_nothing_instead_of_everything():
    assert not matching.contains("anything at all", "") and not matching.contains("anything at all", "   ")
    assert pipeline._matching_keywords(ws_with("Malaki", ["", "  "]), "no brand here") == []


def test_the_workspace_name_and_every_keyword_use_the_looser_rule():
    ws = ws_with("Wave Body Mist", ["Malaki Perfume"])
    assert pipeline._matching_keywords(ws, "my wave bodymist and a malaki-perfume gift") == ["Wave Body Mist", "Malaki Perfume"]
    assert pipeline._matching_keywords(ws, "nothing relevant") == []


def test_the_trial_customers_suggested_keywords_catch_every_post_about_his_brand():
    ws = ws_with("Malaki Studio", ["Wave Body Mist", "Malaki perfume", "malakiperfumes"])
    about = ["Just bought the Wave Body Mist", "Wave Bodymist from Malaki is my favourite", "Is the wave body-mist legit?", "WAVE BODY MIST sold out",
             "Careful: fake sellers use the Malaki Perfumes name", "Order malakiperfumes.com/wave today"]
    assert all(pipeline._matching_keywords(ws, t) for t in about)
    assert not pipeline._matching_keywords(ws, "caught a big wave body surfing") and not pipeline._matching_keywords(ws, "wave body wash on sale")


# ================================================================ 3. the news index's own match is trusted
@pytest.fixture
def classify(monkeypatch):
    monkeypatch.setattr(pipeline, "classify_batch", lambda name, ctx, items: [{"idx": i["idx"], "severity": "WATCH", "sentiment": "neutral", "tags": [], "rationale": "r", "lang": "en"} for i in items])


@pytest.fixture
def workspace(db):
    _, _, ws = make_customer(db)
    ws.name, ws.keywords = "Malaki", ["Wave Body Mist"]
    db.commit()
    return ws


def cand(text, **kw):
    return {"text": text, "url": "https://x.test/" + str(abs(hash(text))), "author": "a", "platform": "News", "posted_at": None, "reach": 0, **kw}


def test_a_story_found_by_the_news_index_is_kept_even_if_the_headline_does_not_repeat_the_name(db, classify, workspace):
    s = pipeline.ingest_candidates(db, workspace, [cand("Fragrance sellers warned over fake perfume sites", source_matched=True)], source="news")
    assert s["new"] == 1
    inc = db.scalar(select(Incident).where(Incident.workspace_id == workspace.id))
    assert inc.title.startswith("Fragrance sellers") and inc.matched_keywords == []


def test_without_that_flag_a_headline_that_does_not_name_the_brand_is_still_dropped(db, classify, workspace):
    assert pipeline.ingest_candidates(db, workspace, [cand("Fragrance sellers warned over fake perfume sites")], source="rss")["new"] == 0


def test_the_headline_match_still_records_which_keyword_matched(db, classify, workspace):
    pipeline.ingest_candidates(db, workspace, [cand("New Wave Bodymist launched in Lagos", source_matched=True)], source="news")
    assert db.scalar(select(Incident).where(Incident.workspace_id == workspace.id)).matched_keywords == ["Wave Body Mist"]


def test_the_news_collector_marks_what_the_index_found(monkeypatch, workspace):
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"articles": [{"title": "Fake perfume sites", "url": "https://n.test/1", "domain": "n.test", "seendate": "20261104T091500Z"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    out = news_collector._gdelt(workspace)
    assert len(out) == 1 and out[0]["source_matched"] is True


# ================================================================ 1. a trial shows its real limits
def trial_account(client, db, email="wave@malaki.com", company="Malaki"):
    trial.save_settings(db, "t", {"enabled": True, "days": 14})
    codes = {}
    import app.services.trial as t
    original = t.send_code_email
    t.send_code_email = lambda row, code: codes.__setitem__(row.email, code) or True
    try:
        assert client.post("/api/public/trial/start", json={"email": email, "name": "Wave Owner", "company": company}).status_code == 200
    finally:
        t.send_code_email = original
    r = client.post("/api/public/trial/verify", json={"email": email, "code": codes[email], "password": PASSWORD})
    assert r.status_code == 200, r.text
    return bearer(r.json()["token"])


def test_a_free_trial_shows_its_real_limits_not_unlimited(client, db):
    h = trial_account(client, db)
    org = client.get("/api/me", headers=h).json()["organization"]
    assert (org["effective_keyword_limit"], org["effective_workspace_limit"]) == (5, 1) and org["keyword_limit"] == 5
    st = client.get("/api/billing/status", headers=h).json()
    assert (st["effective_keyword_limit"], st["effective_workspace_limit"]) == (5, 1)


def test_the_trial_limit_on_screen_follows_what_the_team_sets(client, db):
    h = trial_account(client, db)
    trial.save_settings(db, "t", {"caps": {"keywords": 8, "workspaces": 2}})
    assert client.get("/api/me", headers=h).json()["organization"]["effective_keyword_limit"] == 8


def test_a_paying_customers_limits_are_unchanged(client, db):
    _, token, _ = make_customer(db, plan="growth")
    from app.services.auth import PLAN_KEYWORD_LIMIT, PLAN_WORKSPACE_LIMIT
    org = client.get("/api/me", headers=bearer(token)).json()["organization"]
    assert (org["effective_keyword_limit"], org["effective_workspace_limit"]) == (PLAN_KEYWORD_LIMIT["growth"], PLAN_WORKSPACE_LIMIT["growth"])


def test_the_cap_the_screen_shows_is_the_cap_that_is_enforced(client, db):
    h = trial_account(client, db)
    ws = client.get("/api/me", headers=h).json()["workspaces"][0]["id"]
    five = ["a one", "b two", "c three", "d four", "e five"]
    assert client.patch(f"/api/workspaces/{ws}", headers=h, json={"keywords": five}).status_code == 200
    r = client.patch(f"/api/workspaces/{ws}", headers=h, json={"keywords": five + ["f six"]})
    assert r.status_code == 422 and "allows up to 5" in r.json()["detail"]


# ================================================================ 4. the scan line
def test_a_collector_that_runs_leaves_a_heartbeat_and_one_that_is_not_connected_leaves_nothing(db, classify, workspace, monkeypatch):
    monkeypatch.setattr(worker, "_each_workspace", lambda d: [d.get(Workspace, workspace.id)])
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    worker.run_collector("news", lambda d, w: [cand("Wave Body Mist is trending", source_matched=True)])
    def not_connected(d, w):
        scan_health.note_skipped()
        return []
    worker.run_collector("youtube", not_connected)
    rows = {r.source: r for r in db.scalars(select(ScanRun).where(ScanRun.workspace_id == workspace.id)).all()}
    assert set(rows) == {"news"} and (rows["news"].candidates, rows["news"].new_incidents, rows["news"].error) == (1, 1, "")
    assert scan_health.status_for(db, workspace)["watching"] == ["News"]


def test_a_source_that_could_not_look_is_recorded_as_one_to_retry(db, workspace, monkeypatch):
    monkeypatch.setattr(worker, "_each_workspace", lambda d: [d.get(Workspace, workspace.id)])
    def busy(d, w):
        scan_health.note_problem("The news search was busy")
        return []
    worker.run_collector("news", busy)
    st = scan_health.status_for(db, workspace)
    assert st["watching"] == ["News"] and st["retrying"] == ["News"]


def test_a_collector_that_crashes_is_recorded_not_lost(db, workspace, monkeypatch):
    monkeypatch.setattr(worker, "_each_workspace", lambda d: [d.get(Workspace, workspace.id)])
    def boom(d, w): raise RuntimeError("down")
    worker.run_collector("reddit", boom)
    assert scan_health.status_for(db, workspace)["retrying"] == ["Reddit"]


def test_each_scan_updates_one_row_so_the_table_does_not_grow(db, workspace, monkeypatch):
    monkeypatch.setattr(worker, "_each_workspace", lambda d: [d.get(Workspace, workspace.id)])
    for _ in range(5):
        worker.run_collector("news", lambda d, w: [])
    assert len(db.scalars(select(ScanRun).where(ScanRun.workspace_id == workspace.id, ScanRun.source == "news")).all()) == 1


def test_a_recovered_source_stops_being_listed_as_retrying(db, workspace, monkeypatch):
    monkeypatch.setattr(worker, "_each_workspace", lambda d: [d.get(Workspace, workspace.id)])
    def busy(d, w): scan_health.note_problem("busy"); return []
    worker.run_collector("news", busy); worker.run_collector("news", lambda d, w: [])
    assert scan_health.status_for(db, workspace)["retrying"] == []


def test_old_heartbeats_are_not_shown(db, workspace):
    scan_health.record(db, workspace, "news")
    row = db.scalar(select(ScanRun).where(ScanRun.workspace_id == workspace.id))
    row.finished_at = now_utc() - timedelta(hours=40); db.commit()
    assert scan_health.status_for(db, workspace) == {"has_run": False, "last_scan_at": None, "watching": [], "retrying": []}


def test_sources_are_listed_in_a_steady_order_with_friendly_names(db, workspace):
    for name in ("hackernews", "news", "nairaland", "reddit"):
        scan_health.record(db, workspace, name)
    assert scan_health.status_for(db, workspace)["watching"] == ["News", "Nairaland", "Hacker News", "Reddit"]


def test_the_collectors_own_names_map_to_the_same_source(db, workspace):
    scan_health.record(db, workspace, "news_collector"); scan_health.record(db, workspace, "domain_collector"); scan_health.record(db, workspace, "domains")
    assert scan_health.status_for(db, workspace)["watching"] == ["News", "Domain watch"]


def test_the_status_route_shows_an_account_its_own_workspace_only(client, db):
    _, token, ws = make_customer(db, email="a@one.com", company="One Co")
    _, token2, ws2 = make_customer(db, email="b@two.com", company="Two Co")
    scan_health.record(db, ws, "news")
    got = client.get(f"/api/workspaces/{ws.id}/scan-status", headers=bearer(token)).json()
    assert got["has_run"] is True and got["watching"] == ["News"] and got["demo"] is False and got["last_scan_at"]
    assert client.get(f"/api/workspaces/{ws.id}/scan-status", headers=bearer(token2)).status_code in (403, 404)
    assert client.get(f"/api/workspaces/{ws.id}/scan-status").status_code in (401, 403)


def test_a_brand_new_workspace_says_it_has_not_run_yet(client, db):
    _, token, ws = make_customer(db)
    got = client.get(f"/api/workspaces/{ws.id}/scan-status", headers=bearer(token)).json()
    assert (got["has_run"], got["watching"], got["retrying"], got["last_scan_at"]) == (False, [], [], None)


def test_the_demo_workspace_shows_no_scan_line(client, db):
    from app.services import demo_seed
    org = db.scalar(select(Organization).where(Organization.is_demo == True)) if db.scalar(select(Organization).where(Organization.is_demo == True)) else None   # noqa: E712
    if org is None:
        _, token, ws = make_customer(db, email="demo@x.com", company="Demo Co")
        db.get(Organization, ws.organization_id).is_demo = True; db.commit()
    else:
        ws = db.scalar(select(Workspace).where(Workspace.organization_id == org.id)); token = None
    if token:
        assert client.get(f"/api/workspaces/{ws.id}/scan-status", headers=bearer(token)).json()["demo"] is True


def test_a_manual_full_scan_also_refreshes_the_line(db, classify, workspace, monkeypatch):
    from app import main
    import types
    def quiet_collect(d, w):
        scan_health.note_skipped(); return []
    fake = types.SimpleNamespace(__name__="app.collectors.news_collector", collect=lambda d, w: [cand("Wave Body Mist review", source_matched=True)])
    quiet = types.SimpleNamespace(__name__="app.collectors.youtube_collector", collect=quiet_collect)
    monkeypatch.setattr(main, "COLLECTORS", [fake, quiet])
    main._run_full_scan(workspace.id)
    assert scan_health.status_for(db, workspace)["watching"] == ["News"]


# ---------------------------------------------------------------- the sources report honestly and say nothing when not connected
def test_a_source_that_is_not_configured_says_so_quietly_and_one_that_fails_says_it_could_not_look(monkeypatch, workspace):
    monkeypatch.setattr(youtube_collector, "YOUTUBE_API_KEY", "")
    with scan_health.tracking() as h:
        assert youtube_collector.collect(None, workspace) == []
    assert h["skipped"] is True and h["problem"] == ""
    monkeypatch.setattr(x_collector, "X_ENABLED", False)
    with scan_health.tracking() as h:
        x_collector.collect(None, workspace)
    assert h["skipped"] is True
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    with scan_health.tracking() as h:
        assert hackernews_collector.collect(None, workspace) == []
    assert h["problem"] == "Hacker News could not be reached" and h["skipped"] is False
    with scan_health.tracking() as h:
        assert news_collector._gdelt(workspace) == []
    assert h["problem"] == "The news search could not be reached"


def test_nothing_is_recorded_outside_a_tracked_run():
    scan_health.note_problem("x"); scan_health.note_skipped()          # must simply do nothing
    with scan_health.tracking() as h:
        pass
    assert h == {"problem": "", "skipped": False}
