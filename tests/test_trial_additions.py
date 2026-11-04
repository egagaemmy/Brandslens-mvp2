"""Gaps closed after auditing the trial: one more free provider, Mailchimp for consenting leads only (and in step on unsubscribe),
and the chatbot and Terms no longer contradict the trial."""
import re

import pytest

from app import legal_content
from app.models import TrialSignup
from app.services import chatbot, mailer, trial
from sqlalchemy import select

PASSWORD = "correct-horse-1"


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_email", lambda to, subject, html_body, from_addr="", reply_to="": sent.append({"to": to, "subject": subject, "html": html_body}) or True)
    return sent


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    from app import routes_trial
    for limiter in (routes_trial._start_ip, routes_trial._start_email, routes_trial._verify_ip, routes_trial._request_ip):
        limiter._hits.clear()
    yield


@pytest.fixture
def chimp(monkeypatch):
    calls = {"add": [], "unsub": []}
    monkeypatch.setattr(mailer, "add_to_mailchimp", lambda email, name="": calls["add"].append((email, name)) or True)
    monkeypatch.setattr(mailer, "unsubscribe_from_mailchimp", lambda email: calls["unsub"].append(email) or True)
    return calls


def go(client, db, mails, email="ada@acme-foods.com", consent=True):
    trial.save_settings(db, "test", {"enabled": True})
    assert client.post("/api/public/trial/start", json={"email": email, "name": "Ada Okafor", "company": "Acme Foods", "marketing_consent": consent}).status_code == 200
    mail = next(m for m in reversed(mails) if m["to"] == email and "verification code" in m["subject"])
    code = re.search(r">(\d{6})<", mail["html"]).group(1)
    r = client.post("/api/public/trial/verify", json={"email": email, "code": code, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


def text_of(module):
    return " ".join(v for v in vars(module).values() if isinstance(v, str))


# ---------------------------------------------------------------- who may start a trial
@pytest.mark.parametrize("domain", ["zoho.com", "zoho.eu", "zohomail.com", "zohomail.in"])
def test_free_zoho_addresses_cannot_start_a_trial(domain):
    assert trial.is_business_email(f"a@{domain}")[0] is False


def test_a_company_using_zoho_with_its_own_domain_still_can():
    assert trial.is_business_email("ada@acme-foods.com")[0] is True and trial.is_business_email("ada@zoho-partners.co.ng")[0] is True


# ---------------------------------------------------------------- Mailchimp
def test_a_lead_who_agreed_to_marketing_emails_joins_mailchimp_with_their_first_name(client, db, mails, chimp):
    go(client, db, mails, consent=True)
    assert chimp["add"] == [("ada@acme-foods.com", "Ada")]


def test_a_lead_who_did_not_agree_is_never_sent_to_mailchimp(client, db, mails, chimp):
    go(client, db, mails, consent=False)
    assert chimp["add"] == []


def test_mailchimp_being_down_does_not_break_the_sign_up(client, db, mails, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Mailchimp is down")
    monkeypatch.setattr(mailer, "add_to_mailchimp", boom)
    body = go(client, db, mails, consent=True)
    assert body["token"] and body["trial_ends_at"]


def test_unsubscribing_through_the_email_link_also_unsubscribes_in_mailchimp(client, db, mails, chimp):
    go(client, db, mails, consent=True)
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@acme-foods.com"))
    assert client.get(f"/api/public/trial/unsubscribe?token={row.unsub_token}").status_code == 200
    assert chimp["unsub"] == ["ada@acme-foods.com"]
    db.refresh(row)
    assert row.marketing_consent is False and row.unsubscribed_at is not None


def test_a_bad_unsubscribe_link_changes_nothing_in_mailchimp(client, db, mails, chimp):
    go(client, db, mails, consent=True)
    client.get("/api/public/trial/unsubscribe?token=not-a-real-token")
    assert chimp["unsub"] == []


def test_mailchimp_unsubscribe_does_nothing_when_mailchimp_is_not_set_up(monkeypatch):
    monkeypatch.setattr(mailer, "MAILCHIMP_API_KEY", "")
    monkeypatch.setattr(mailer.httpx, "patch", lambda *a, **k: pytest.fail("no request should be made"))
    assert mailer.unsubscribe_from_mailchimp("a@b.com") is False


def test_mailchimp_unsubscribe_sends_the_right_request(monkeypatch):
    for name, value in (("MAILCHIMP_API_KEY", "key-us1"), ("MAILCHIMP_SERVER_PREFIX", "us1"), ("MAILCHIMP_LIST_ID", "list9")):
        monkeypatch.setattr(mailer, name, value)
    seen = {}

    class Ok:
        status_code = 200
        def raise_for_status(self): pass

    def fake(url, **kw):
        seen.update(url=url, **kw)
        return Ok()
    monkeypatch.setattr(mailer.httpx, "patch", fake)
    assert mailer.unsubscribe_from_mailchimp("Ada@Acme-Foods.com") is True
    import hashlib
    assert seen["url"].endswith("/lists/list9/members/" + hashlib.md5(b"ada@acme-foods.com").hexdigest())     # same lower-cased hash the subscribe call uses
    assert seen["json"] == {"status": "unsubscribed"} and seen["auth"] == ("anystring", "key-us1")


def test_someone_who_was_never_on_the_mailchimp_list_is_not_an_error(monkeypatch):
    for name, value in (("MAILCHIMP_API_KEY", "k"), ("MAILCHIMP_SERVER_PREFIX", "us1"), ("MAILCHIMP_LIST_ID", "l")):
        monkeypatch.setattr(mailer, name, value)

    class Missing:
        status_code = 404
        def raise_for_status(self): raise AssertionError("a 404 must not be treated as a failure")
    monkeypatch.setattr(mailer.httpx, "patch", lambda *a, **k: Missing())
    assert mailer.unsubscribe_from_mailchimp("x@y.com") is False


def test_a_mailchimp_failure_on_unsubscribe_is_swallowed(monkeypatch):
    for name, value in (("MAILCHIMP_API_KEY", "k"), ("MAILCHIMP_SERVER_PREFIX", "us1"), ("MAILCHIMP_LIST_ID", "l")):
        monkeypatch.setattr(mailer, name, value)
    def boom(*a, **k): raise RuntimeError("network down")
    monkeypatch.setattr(mailer.httpx, "patch", boom)
    assert mailer.unsubscribe_from_mailchimp("x@y.com") is False


# ---------------------------------------------------------------- wording that must not contradict the trial
def test_the_chatbot_no_longer_tells_visitors_there_is_no_trial():
    t = text_of(chatbot)
    assert "There is NO free trial" not in t
    assert "www.brandslens.com/pricing" in t and "Never state a length" in t


def test_the_terms_describe_the_trial_that_really_exists():
    t = text_of(legal_content)
    assert "convert to a paid subscription unless" not in t
    assert "needs no payment card and never turns into a paid plan" in t and "unsubscribe at any time" in t


# ---------------------------------------------------------------- the email sequence: one short story, signed Pat, building toward a plan
OLD_DESIGN = {"welcome": ("transactional", "after_start", 0), "day1_setup": ("marketing", "after_start", 1), "day3_clock": ("marketing", "after_start", 3),
              "day7_respond": ("marketing", "after_start", 7), "day14_prove": ("marketing", "after_start", 14), "before_end_7": ("marketing", "before_end", 7),
              "before_end_3": ("transactional", "before_end", 3), "before_end_1": ("transactional", "before_end", 1), "ended": ("transactional", "after_end", 0),
              "after_end_3": ("marketing", "after_end", 3), "after_end_10": ("marketing", "after_end", 10)}
STORY = {"day1_setup", "day3_clock", "day7_respond", "day14_prove", "after_end_3"}
PLAN_LINK = {"before_end_7", "before_end_3", "before_end_1", "ended", "after_end_3", "after_end_10"}


def lead(client, db, mails):
    go(client, db, mails)
    return db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@acme-foods.com"))


def rendered(db, row):
    return {s["key"]: trial.build_email(db, row, s) for s in trial.steps(db)}


def test_every_timing_and_every_type_is_unchanged_so_nothing_goes_to_the_wrong_people(db):
    assert {s["key"]: (s["kind"], s["when"], s["days"]) for s in trial.DEFAULT_STEPS} == OLD_DESIGN


def test_every_email_is_signed_pat_from_brandslens_and_never_the_founder(client, db, mails):
    row = lead(client, db, mails)
    for key, (subject, body) in rendered(db, row).items():
        assert "Pat from BrandsLens" in body, key
        assert "Emmanuel" not in body and "Founder" not in body, key
    welcome = next(m for m in mails if m["to"] == "ada@acme-foods.com" and "trial is live" in m["subject"])
    assert "Pat here, from BrandsLens" in welcome["html"] and "Emmanuel" not in welcome["html"]


def test_the_story_runs_through_the_marketing_emails_and_is_clearly_invented(client, db, mails):
    row = lead(client, db, mails)
    out = rendered(db, row)
    for key in STORY:
        assert "Mara" in out[key][1], key
    assert "Mara is made up" in out["day1_setup"][1]


def test_the_emails_about_the_trial_itself_stay_plain_because_they_go_to_everyone(client, db, mails):
    row = lead(client, db, mails)
    out = rendered(db, row)
    for key, (kind, _, _) in OLD_DESIGN.items():
        if kind == "transactional":
            assert "Mara" not in out[key][1], key


def test_the_push_toward_a_paid_plan_builds_as_the_days_go_by(client, db, mails):
    row = lead(client, db, mails)
    out = rendered(db, row)
    plan_buttons = {k for k, (_, body) in out.items() if "/billing" in body}
    assert plan_buttons == PLAN_LINK
    for early in ("welcome", "day1_setup", "day3_clock", "day7_respond"):
        assert "/billing" not in out[early][1] and "Open your dashboard" in out[early][1]       # the early emails teach and tell the story
    assert "full plan" in out["day14_prove"][1]                                               # the first soft mention of a plan
    for late in ("before_end_7", "ended", "after_end_3"):
        assert "Choose a plan" in out[late][1]


def test_with_an_offer_code_the_button_carries_it_and_the_line_says_so(client, db, mails):
    trial.save_settings(db, "t", {"offer_code": "trial15"})
    row = lead(client, db, mails)
    out = rendered(db, row)
    for key in PLAN_LINK - {"after_end_10"}:
        assert "billing?promo=TRIAL15" in out[key][1] and "Your offer code <b>TRIAL15</b> is already filled in" in out[key][1], key
    assert "billing?promo=TRIAL15" in out["after_end_10"][1]


def test_without_an_offer_code_no_email_mentions_one(client, db, mails):
    row = lead(client, db, mails)
    for key, (subject, body) in rendered(db, row).items():
        assert "offer code" not in body.lower() and "promo=" not in body, key
        assert "{{" not in body and "{{" not in subject, key                                   # every placeholder was filled in


def test_the_plan_facts_in_the_emails_match_the_pricing(client, db, mails):
    row = lead(client, db, mails)
    body = rendered(db, row)["before_end_7"][1]
    assert "one workspace and five keywords" in body and "Standard watches 25 keywords" in body and "Growth and Professional" in body


def test_no_email_makes_a_claim_it_cannot_back_up(client, db, mails):
    row = lead(client, db, mails)
    for key, (subject, body) in rendered(db, row).items():
        text = (subject + " " + body).lower()
        for banned in ("our customers", "customers tell us", "guarantee", "trusted by", "%", "testimonial", "award"):
            assert banned not in text, (key, banned)


def test_the_subjects_are_short_human_and_not_shouting(client, db, mails):
    row = lead(client, db, mails)
    for key, (subject, _) in rendered(db, row).items():
        assert 0 < len(subject) <= 60, (key, subject)
        assert not any(w.isupper() and len(w) > 3 for w in re.findall(r"[A-Za-z]+", subject)), (key, subject)
        assert "!!" not in subject and "free!" not in subject.lower(), (key, subject)


# ---------------------------------------------------------------- the email sequence: honest numbers, one voice, no promotion without consent
from datetime import timedelta
from app import config
from app.services.trial import aware


def _lead(db, email):
    return db.scalar(select(TrialSignup).where(TrialSignup.email == email))


def _step(db, key):
    return next(s for s in trial.steps(db) if s["key"] == key)


def test_the_phrases_follow_the_real_limits_and_time_left():
    assert trial.keywords_phrase(5) == "up to five keywords" and trial.keywords_phrase(3) == "up to three keywords" and trial.keywords_phrase(1) == "one keyword"
    assert trial.keywords_phrase(12) == "up to 12 keywords"
    assert trial.limits_phrase(1, 5) == "one workspace and five keywords" and trial.limits_phrase(2, 12) == "two workspaces and 12 keywords"
    assert trial.days_phrase(1) == "1 day" and trial.days_phrase(16) == "16 days" and trial.days_phrase(-3) == "0 days"


def test_the_welcome_and_one_week_left_emails_state_the_real_limits(client, db, mails, chimp):
    go(client, db, mails)
    row = _lead(db, "ada@acme-foods.com")
    _, welcome = trial.build_email(db, row, _step(db, "welcome"))
    _, week = trial.build_email(db, row, _step(db, "before_end_7"))
    assert "up to five keywords" in welcome and "starter" not in welcome and "{{" not in welcome
    assert "one workspace and five keywords" in week and "{{" not in week


def test_the_day_14_email_says_how_many_days_are_really_left_not_two_weeks(client, db, mails, chimp):
    go(client, db, mails)
    row = _lead(db, "ada@acme-foods.com")
    row.ends_at = aware(row.started_at) + timedelta(days=14, hours=2, minutes=30)
    db.commit()
    _, body = trial.build_email(db, row, _step(db, "day14_prove"))
    assert "about two weeks left" not in body
    row.ends_at = trial._now(None) + timedelta(hours=20)
    db.commit()
    _, last = trial.build_email(db, row, _step(db, "day14_prove"))
    assert "1 day left" in last and "1 days" not in last


def test_a_service_email_carries_no_offer_for_someone_who_did_not_agree_to_marketing(client, db, mails, chimp):
    trial.save_settings(db, "test", {"offer_code": "TRIAL15"})
    go(client, db, mails, email="ada@acme-foods.com", consent=False)
    row = _lead(db, "ada@acme-foods.com")
    _, html = trial.build_email(db, row, _step(db, "before_end_3"))
    assert "TRIAL15" not in html and "?promo=" not in html and "/billing" in html and "Unsubscribe" not in html


def test_someone_who_agreed_gets_the_offer_in_the_link_and_the_line_and_an_unsubscribe(client, db, mails, chimp):
    trial.save_settings(db, "test", {"offer_code": "TRIAL15"})
    go(client, db, mails, email="bola@other-co.com", consent=True)
    row = _lead(db, "bola@other-co.com")
    _, service = trial.build_email(db, row, _step(db, "before_end_3"))
    _, marketing = trial.build_email(db, row, _step(db, "before_end_7"))
    assert "TRIAL15" in service and "/billing?promo=TRIAL15" in service
    assert "TRIAL15" in marketing and "/billing?promo=TRIAL15" in marketing and "Unsubscribe" in marketing


def test_every_default_email_is_signed_pat_and_never_carries_the_founders_name():
    for s in trial.DEFAULT_STEPS:
        assert "Pat from BrandsLens" in s["body"], s["key"]
        assert not re.search(r"Emmanuel|Egaga|Emmy", s["subject"] + s["body"]), s["key"]


def test_the_sender_name_is_pat_from_brandslens():
    assert config.TRIAL_FROM.startswith("Pat from BrandsLens <") and config.TRIAL_FROM.endswith("@mail.brandslens.com>")
    assert not re.search(r"Emmanuel|Egaga|Emmy", config.TRIAL_FROM)


def test_the_story_uses_a_made_up_person_and_says_so():
    """Honesty: the sequence tells a story about a character, never a real customer, and must say plainly that she is made up."""
    day1 = _body_text(_default("day1_setup"))
    assert "Mara is made up" in day1
    assert not re.search(r"\b(our customer|a customer told us|testimonial)\b", " ".join(s["body"] for s in trial.DEFAULT_STEPS), re.I)


def _default(key):
    return next(s for s in trial.DEFAULT_STEPS if s["key"] == key)


def _body_text(step):
    return re.sub(r"<[^>]+>", " ", step["body"])


def test_the_day_1_email_tells_the_story_from_the_problem_to_the_solution_and_says_mara_is_made_up():
    """The follow-up emails may open on the problem: the story must then arrive at what BrandsLens does about it."""
    body = _body_text(_default("day1_setup"))
    assert "Mara finds out" in body and "exactly the moment BrandsLens exists to prevent" in body      # problem, then the solution
    assert "Mara is made up" in body and "three minutes" in body and "{{company}}" in _default("day1_setup")["body"]


# ---------------------------------------------------------------- job title and location on the form
from app.models import OrgMember


def start_with(client, db, mails, email="ada@acme-foods.com", **extra):
    trial.save_settings(db, "test", {"enabled": True})
    body = {"email": email, "name": "Ada Okafor", "company": "Acme Foods", "marketing_consent": False, **extra}
    r = client.post("/api/public/trial/start", json=body)
    assert r.status_code == 200, r.text
    return r


def verify_for(client, mails, email="ada@acme-foods.com"):
    mail = next(m for m in reversed(mails) if m["to"] == email and "verification code" in m["subject"])
    code = re.search(r">(\d{6})<", mail["html"]).group(1)
    r = client.post("/api/public/trial/verify", json={"email": email, "code": code, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r


def test_the_job_title_and_location_are_stored_tidied(client, db, mails, chimp):
    start_with(client, db, mails, job_title="  Head   of  Communications ", location=" Lagos,   Nigeria ")
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@acme-foods.com"))
    assert (row.job_title, row.location) == ("Head of Communications", "Lagos, Nigeria")


def test_an_older_form_that_sends_neither_still_works(client, db, mails, chimp):
    start_with(client, db, mails)
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@acme-foods.com"))
    assert (row.job_title, row.location) == ("", "")


def test_odd_characters_are_removed_and_long_text_is_cut(client, db, mails, chimp):
    start_with(client, db, mails, job_title="Head\x00 of\n\tComms" + "x" * 400, location="Lagos\x07, Nigeria")
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@acme-foods.com"))
    assert "\x00" not in row.job_title and "\n" not in row.job_title and len(row.job_title) <= 160 and row.job_title.startswith("Head of Comms")
    assert row.location == "Lagos , Nigeria".replace(" ,", ",") or row.location.startswith("Lagos")


def test_a_second_attempt_with_blank_fields_does_not_erase_what_was_given(client, db, mails, chimp):
    start_with(client, db, mails, job_title="CMO", location="Accra, Ghana")
    later = trial._now(None) + timedelta(minutes=5)                 # past the one-minute wait before another code can be sent
    trial.start(db, email="ada@acme-foods.com", name="Ada Okafor", company="Acme Foods", consent=False, now=later)
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@acme-foods.com"))
    assert (row.job_title, row.location) == ("CMO", "Accra, Ghana")


def test_the_designation_and_a_clear_city_and_country_reach_the_new_owner_profile(client, db, mails, chimp):
    start_with(client, db, mails, job_title="Head of Communications", location="Lagos, Nigeria")
    verify_for(client, mails)
    owner = db.scalar(select(OrgMember).where(OrgMember.email == "ada@acme-foods.com"))
    assert (owner.job_title, owner.city, owner.country) == ("Head of Communications", "Lagos", "Nigeria")


def test_a_single_word_location_is_not_guessed_into_a_city_or_country(client, db, mails, chimp):
    start_with(client, db, mails, job_title="CMO", location="Nigeria")
    verify_for(client, mails)
    owner = db.scalar(select(OrgMember).where(OrgMember.email == "ada@acme-foods.com"))
    assert owner.job_title == "CMO" and (owner.city, owner.country) == ("", "")
    assert trial.split_location("Port Harcourt, Rivers, Nigeria") == ("Port Harcourt, Rivers", "Nigeria")


def test_someone_asking_for_access_without_a_business_email_gives_them_too(client, db, mails):
    trial.save_settings(db, "test", {"enabled": True})
    r = client.post("/api/public/trial/request-access", json={"email": "ada@gmail.com", "name": "Ada Okafor", "company": "Acme Foods", "job_title": "Brand Manager", "location": "Nairobi, Kenya", "note": "No work email yet"})
    assert r.status_code == 200, r.text
    row = db.scalar(select(TrialSignup).where(TrialSignup.email == "ada@gmail.com"))
    assert (row.status, row.job_title, row.location) == ("requested", "Brand Manager", "Nairobi, Kenya")
    team = next(m for m in mails if m["to"] == "info@brandslens.com")
    assert "Brand Manager" in team["html"] and "Nairobi, Kenya" in team["html"]


def test_the_team_notice_for_a_new_trial_names_the_title_and_location_and_escapes_them(client, db, mails, chimp):
    start_with(client, db, mails, job_title="<b>Boss</b>", location="Lagos, Nigeria")
    verify_for(client, mails)
    notice = next(m for m in mails if m["to"] == "info@brandslens.com" and "New trial" in m["subject"])
    assert "&lt;b&gt;Boss&lt;/b&gt;" in notice["html"] and "Lagos, Nigeria" in notice["html"] and "<b>Boss</b>" not in notice["html"]


def test_the_lead_list_shows_them_and_can_be_searched_by_either(client, db, mails, chimp):
    start_with(client, db, mails, job_title="Head of Communications", location="Lagos, Nigeria")
    got = trial.list_signups(db)["rows"][0]
    assert (got["job_title"], got["location"]) == ("Head of Communications", "Lagos, Nigeria")
    assert trial.list_signups(db, q="lagos")["total"] == 1 and trial.list_signups(db, q="communications")["total"] == 1 and trial.list_signups(db, q="zzz")["total"] == 0


def test_the_csv_has_both_columns_and_a_typed_formula_cannot_run(client, db, mails, chimp):
    import csv as _csv, io as _io
    start_with(client, db, mails, job_title="=HYPERLINK(\"http://evil\")", location="Lagos, Nigeria")
    rows = list(_csv.DictReader(_io.StringIO(trial.export_csv(db))))
    assert list(rows[0]).index("job_title") == list(rows[0]).index("company") + 1 and list(rows[0]).index("location") == list(rows[0]).index("job_title") + 1
    assert rows[0]["location"] == "Lagos, Nigeria" and rows[0]["job_title"].startswith("'=")      # the leading quote stops a spreadsheet running it


def test_the_default_privacy_policy_says_the_trial_form_collects_job_title_and_location():
    t = text_of(legal_content)
    assert "Free trial details" in t and "job\n  title and location" in t

