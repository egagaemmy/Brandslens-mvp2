"""Shared helpers for the AI self tests: a stand in for the Anthropic API that can simulate each failure."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ADMIN_SETUP_SECRET", "s3cret")
import httpx, anthropic

import app.services.ai as ai
import app.services.chatbot as chatbot

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
def make(cls, status, msg):
    return cls(msg, response=httpx.Response(status, request=REQ), body=None)

class Block:
    def __init__(self, t): self.type, self.text = "text", t
class Resp:
    def __init__(self, t): self.content = [Block(t)]

class Fake:
    """messages.create replacement. `plan` maps model -> an exception to raise, or text to return."""
    def __init__(self, plan, default="Hello from the model."):
        self.plan, self.default, self.calls = plan, default, []
        self.messages = self
    def create(self, model, max_tokens, messages, system=None, **kw):
        self.calls.append({"model": model, "messages": messages})
        r = self.plan.get(model, self.default)
        if isinstance(r, Exception): raise r
        return Resp(r)

def install(fake, key="sk-ant-api03-ABCDEFGHIJKLMNOP-wxyz", configured="claude-haiku-5-5"):
    ai._client = lambda: fake; ai.ANTHROPIC_API_KEY = key; ai.CLASSIFIER_MODEL = configured
    chatbot.ANTHROPIC_API_KEY = key; ai._working_model = None
    for mod in ("app.services.classifier", "app.services.media_room"):
        pass
    import app.config as cfg; cfg.ANTHROPIC_API_KEY = key
    import app.services.classifier as cl; cl.ANTHROPIC_API_KEY = key


FAILS = [0]
def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    FAILS[0] += 0 if cond else 1
