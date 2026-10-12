"""Self test for the AI layer. Needs no real key: a stand in for the API simulates each failure.
Run from the project folder:  python scripts/test_ai.py"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_testkit import *
import anthropic, httpx
import app.services.ai as ai
import app.services.chatbot as chatbot


# 1. a retired configured model: falls through to the next one and remembers it
f = Fake({"claude-haiku-5-5": make(anthropic.NotFoundError, 404, "model: claude-haiku-5-5 not found")}); install(f)
ok("retired model -> next candidate answers", ai.complete("s", [{"role": "user", "content": "hi"}], 50) == "Hello from the model.", [c["model"] for c in f.calls])
ok("the working model is remembered and tried first next time", ai.candidate_models()[0] == "claude-sonnet-5-5", ai.candidate_models()[0])
f.calls.clear(); ai.complete("s", [{"role": "user", "content": "hi"}], 50)
ok("second call goes straight to it (no repeat of the 404)", [c["model"] for c in f.calls] == ["claude-sonnet-5-5"])
# 2. a configured model name that is simply wrong
f = Fake({"totally-made-up": make(anthropic.NotFoundError, 404, "model not found")}); install(f, configured="totally-made-up")
ok("a mistyped CLASSIFIER_MODEL no longer breaks the app", ai.complete("s", [{"role": "user", "content": "hi"}], 5) == "Hello from the model.")
# 3. failures that would be identical on every model stop immediately, with the right kind
for label, exc, kind in (("bad key", make(anthropic.AuthenticationError, 401, "invalid x-api-key"), "auth"),
                         ("no credit", make(anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API"), "billing"),
                         ("rate limit", make(anthropic.RateLimitError, 429, "rate limited"), "rate_limit"),
                         ("outage", anthropic.APIConnectionError(request=REQ), "connection")):
    f = Fake({m: exc for m in ai.candidate_models()}); install(f)
    try: ai.complete("s", [{"role": "user", "content": "hi"}], 5); got = "no error"
    except ai.AIUnavailable as e: got = e.kind
    ok(f"{label} -> kind '{kind}'", got == kind, got)
    if kind != "connection": ok(f"  ...and only one model was tried (it would fail on all)", len(f.calls) == 1, f"{len(f.calls)} calls")
# 4. a busy model falls through
f = Fake({"claude-haiku-5-5": make(anthropic.InternalServerError, 529, "overloaded")}); install(f)
ok("overloaded model -> next candidate", ai.complete("s", [{"role": "user", "content": "hi"}], 5) == "Hello from the model.")
# 5. no key
install(Fake({}), key="")
try: ai.complete("s", [{"role": "user", "content": "hi"}], 5); got = "no error"
except ai.AIUnavailable as e: got = e.kind
ok("no key -> 'not_configured'", got == "not_configured", got)

# 6. the Ask bot: the history bug and the honest messages
f = Fake({}, default="Four HIGH mentions this week."); install(f)
long_history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(9)]   # last six start with an ASSISTANT turn
r = chatbot.answer_app_chat("What is urgent?", long_history, "Acme", "basic", "Total mentions: 3")
sent = f.calls[0]["messages"]
ok("long conversation: the first message sent is from the user (this used to fail)", sent[0]["role"] == "user", [m["role"] for m in sent])
ok("the answer comes back", r == "Four HIGH mentions this week.")
ok("the new question is last", sent[-1] == {"role": "user", "content": "What is urgent?"})
for label, exc, expect in (("bad key", make(anthropic.AuthenticationError, 401, "bad"), "temporarily unavailable"),
                           ("rate limit", make(anthropic.RateLimitError, 429, "slow"), "lot of questions"),
                           ("outage", anthropic.APIConnectionError(request=REQ), "couldn't reach")):
    install(Fake({m: exc for m in ai.candidate_models()}))
    ok(f"Ask with {label} says something honest and specific", expect in chatbot.answer_app_chat("hi", [], "Acme", "basic", "x"), chatbot.answer_app_chat("hi", [], "Acme", "basic", "x")[:50])
install(Fake({}), key="")
ok("Ask with no key keeps its original message", "not fully connected" in chatbot.answer_app_chat("hi", [], "Acme", "basic", "x"))

# 7. the other features keep their graceful fallbacks, and now benefit from model fallback
import app.services.classifier as cl, app.services.media_room as mr
items = [{"idx": 0, "platform": "News", "text": "Fake account impersonating the brand"}]
good = json.dumps([{"idx": "0", "severity": "HIGH", "sentiment": "negative", "tags": ["IMPERSONATION"], "reason": "x"}])
f = Fake({"claude-haiku-5-5": make(anthropic.NotFoundError, 404, "gone")}, default=good); install(f)
res = cl.classify_batch("Acme", "foods", items)
ok("classifier survives a retired model and still classifies HIGH", res[0]["severity"] == "HIGH", res[0].get("severity"))
install(Fake({m: make(anthropic.AuthenticationError, 401, "bad") for m in ai.candidate_models()}))
ok("classifier with a bad key still returns an item (WATCH fallback)", cl.classify_batch("Acme", "foods", items)[0]["severity"] == "WATCH")
install(Fake({}, default="DRAFT TEXT"))
ok("Media Room draft uses the AI text", mr.draft_statement_ai("Acme", "summary", "holding") == "DRAFT TEXT")
install(Fake({m: make(anthropic.AuthenticationError, 401, "bad") for m in ai.candidate_models()}))
ok("Media Room draft with a bad key falls back to the template", "AI assist unavailable" in mr.draft_statement_ai("Acme", "summary", "holding"))

# 8. the diagnostic route
from fastapi.testclient import TestClient
import app.main as m
c = TestClient(m.app)
ok("ai-check refuses without the secret", c.get("/api/setup/ai-check", params={"secret": "wrong"}).status_code == 403)
install(Fake({})); j = c.get("/api/setup/ai-check", params={"secret": "s3cret"}).json()
ok("ai-check: working key -> 'Working'", j["verdict"].startswith("Working"), j["verdict"][:60])
ok("ai-check never reveals the key", "ABCDEFGHIJKLMNOP" not in json.dumps(j) and j["key_hint"] == "sk-ant-…wxyz", j["key_hint"])
install(Fake({m: make(anthropic.AuthenticationError, 401, "invalid x-api-key") for m in ai.candidate_models()}))
j = c.get("/api/setup/ai-check", params={"secret": "s3cret"}).json()
ok("ai-check: rejected key -> plain instruction", "key was rejected" in j["verdict"] and len(j["models"]) == 1, j["verdict"][:70])
install(Fake({m: make(anthropic.BadRequestError, 400, "Your credit balance is too low") for m in ai.candidate_models()}))
ok("ai-check: no credit -> plain instruction", "no usable credit" in c.get("/api/setup/ai-check", params={"secret": "s3cret"}).json()["verdict"])
install(Fake({"claude-haiku-5-5": make(anthropic.NotFoundError, 404, "gone")}), configured="claude-haiku-5-5")
ok("ai-check: configured model dead but fallback works -> says so", "update CLASSIFIER_MODEL" in c.get("/api/setup/ai-check", params={"secret": "s3cret"}).json()["verdict"])
install(Fake({}), key=""); ok("ai-check: no key -> says so", "not set" in c.get("/api/setup/ai-check", params={"secret": "s3cret"}).json()["verdict"])
print("\nALL AI CHECKS PASSED" if not FAILS[0] else f"\n{FAILS[0]} CHECK(S) FAILED"); sys.exit(1 if FAILS[0] else 0)
