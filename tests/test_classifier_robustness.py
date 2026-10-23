"""The classifier must never lose a batch because the AI's reply was long, wrapped in text, or cut off. Complete items are kept and only
the missing ones are asked for again; an account problem is not retried; and the number of repeat calls is strictly limited."""
import json

import pytest

from app.services import ai, classifier


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(classifier, "ANTHROPIC_API_KEY", "sk-ant-test")


def items(n):
    return [{"idx": i, "text": f"Mention number {i}", "platform": "X"} for i in range(n)]


def obj(i, sev="WATCH", rationale="Short reason."):
    return {"idx": i, "severity": sev, "sentiment": "Negative", "tags": ["FRAUD"] if sev == "HIGH" else [], "lang": "en", "rationale": rationale}


def reply(objs, indent=None):
    return json.dumps(objs, indent=indent)


class Script:
    """Replays the AI's answers in order and records every call."""
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, system, messages, max_tokens):
        self.calls.append({"content": messages[0]["content"], "max_tokens": max_tokens})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def use(monkeypatch, *answers):
    s = Script(answers)
    monkeypatch.setattr(classifier.ai, "complete", s)
    return s


def run(n):
    return classifier.classify_batch("Acme", "Sector: food.", items(n))


def is_fallback(r):
    return r["rationale"].startswith("Fallback")


# ---------------------------------------------------------------- reading the reply
def test_a_clean_reply_is_read(monkeypatch):
    s = use(monkeypatch, reply([obj(0, "HIGH"), obj(1)]))
    out = run(2)
    assert [r["severity"] for r in out] == ["HIGH", "WATCH"] and len(s.calls) == 1


def test_an_indented_reply_is_read(monkeypatch):
    use(monkeypatch, reply([obj(0, "HIGH"), obj(1, "MEDIUM")], indent=4))
    assert [r["severity"] for r in run(2)] == ["HIGH", "MEDIUM"]


def test_prose_and_code_fences_around_the_answer_are_ignored(monkeypatch):
    use(monkeypatch, "Here is the classification:\n```json\n" + reply([obj(0, "HIGH"), obj(1)], indent=2) + "\n```\nHope that helps.")
    assert [r["severity"] for r in run(2)] == ["HIGH", "WATCH"]


def test_an_id_given_as_text_still_matches(monkeypatch):
    use(monkeypatch, reply([{**obj(0, "HIGH"), "idx": "0"}, {**obj(1), "idx": "1"}]))
    assert [r["severity"] for r in run(2)] == ["HIGH", "WATCH"]


def test_the_reader_keeps_complete_objects_and_drops_the_cut_off_one():
    full = reply([obj(0, "HIGH"), obj(1, "MEDIUM", "A rationale that goes on"), obj(2)], indent=2)
    cut = full[: full.rindex('"rationale"') + 18]
    assert [o["idx"] for o in classifier._read_items(cut)] == [0, 1]
    assert [o["idx"] for o in classifier._read_items(full)] == [0, 1, 2]
    assert classifier._read_items("") == [] and classifier._read_items("no array here") == [] and classifier._read_items(None) == []
    assert classifier._read_items('[1, "x", {"idx": 3}]') == [{"idx": 3}]                 # anything that is not an object is skipped


# ---------------------------------------------------------------- the failure that actually happened
def test_a_reply_cut_off_mid_sentence_keeps_what_was_complete_and_asks_again_for_only_the_rest(monkeypatch):
    full = reply([obj(0, "HIGH"), obj(1, "MEDIUM"), obj(2, "HIGH", "A long rationale for the third item")], indent=2)
    cut = full[: full.rindex('"rationale"') + 25]                       # ends inside a string, exactly like the real log
    with pytest.raises(json.JSONDecodeError):
        json.loads(cut)
    s = use(monkeypatch, cut, reply([obj(2, "HIGH", "second try")]))
    out = run(3)
    assert [r["severity"] for r in out] == ["HIGH", "MEDIUM", "HIGH"] and out[2]["rationale"] == "second try"
    assert not any(is_fallback(r) for r in out)
    assert len(s.calls) == 2
    assert "Mention number 2" in s.calls[1]["content"] and "Mention number 0" not in s.calls[1]["content"]      # only the missing one was re-sent


def test_a_valid_reply_that_skips_an_item_asks_again_for_that_item(monkeypatch):
    s = use(monkeypatch, reply([obj(0), obj(2)]), reply([obj(1, "HIGH")]))
    out = run(3)
    assert [r["severity"] for r in out] == ["WATCH", "HIGH", "WATCH"] and len(s.calls) == 2


def test_when_nothing_is_readable_each_half_is_tried_on_its_own(monkeypatch):
    s = use(monkeypatch, "Sorry, I cannot do that.", reply([obj(0), obj(1, "HIGH")]), reply([obj(2), obj(3, "MEDIUM")]))
    out = run(4)
    assert [r["severity"] for r in out] == ["WATCH", "HIGH", "WATCH", "MEDIUM"] and len(s.calls) == 3
    assert "Mention number 3" not in s.calls[1]["content"] and "Mention number 0" not in s.calls[2]["content"]


# ---------------------------------------------------------------- limits and honesty
def test_endless_garbage_ends_in_fallback_after_a_strictly_limited_number_of_calls(monkeypatch):
    s = use(monkeypatch, *["nope"] * 50)
    out = run(4)
    assert all(is_fallback(r) for r in out) and 1 <= len(s.calls) <= 7
    one = use(monkeypatch, *["nope"] * 50)
    assert is_fallback(run(1)[0]) and len(one.calls) <= 3


def test_an_account_problem_is_never_retried(monkeypatch):
    s = use(monkeypatch, ai.AIUnavailable("credit", "no credit"))
    out = run(5)
    assert all(is_fallback(r) for r in out) and len(s.calls) == 1


def test_no_key_means_fallback_and_no_call(monkeypatch):
    monkeypatch.setattr(classifier, "ANTHROPIC_API_KEY", "")
    s = use(monkeypatch)
    assert all(is_fallback(r) for r in run(3)) and s.calls == []


def test_an_empty_batch_is_a_no_op(monkeypatch):
    s = use(monkeypatch)
    assert classifier.classify_batch("Acme", "x", []) == [] and s.calls == []


def test_the_reply_allowance_grows_with_the_batch_and_has_a_ceiling(monkeypatch):
    assert classifier._tokens_for(1) == 650 and classifier._tokens_for(15) == 4150 and classifier._tokens_for(15) > 1500
    assert classifier._tokens_for(40) == 8000 and classifier._tokens_for(400) == 8000
    s = use(monkeypatch, reply([obj(i) for i in range(15)]))
    run(15)
    assert s.calls[0]["max_tokens"] == 4150


def test_the_instructions_ask_for_compact_short_answers():
    assert "compact JSON" in classifier.SYSTEM and "under 15 words" in classifier.SYSTEM
