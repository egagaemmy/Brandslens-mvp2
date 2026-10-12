"""app/services/ai.py — the ONE place every AI call goes through.

Before this existed, five different features each made their own call to a single
hard coded model and each hid any failure behind a vague message or a silent fallback.
So a retired model, a rejected key or used up credit looked identical from the
outside ("Something went wrong"), and the real cause was only visible in the host's
logs. Now:

  * if the configured model has been retired or renamed, the next candidate is tried
    automatically, and the one that worked is remembered;
  * every failure is classified (key, credit, rate limit, model, connection) and logged
    with its real type and message;
  * /api/setup/ai-check shows exactly which of those it is, without needing the logs.
"""
from __future__ import annotations
import logging
import time

from anthropic import (Anthropic, APIConnectionError, APIStatusError, AuthenticationError,
                       BadRequestError, NotFoundError, PermissionDeniedError, RateLimitError)

from ..config import ANTHROPIC_API_KEY, CLASSIFIER_MODEL

log = logging.getLogger("ai")

# Tried in this order after the configured model. Current, then older, so that an
# account without access to one still gets an answer from another.
FALLBACK_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-haiku-4-5")

_working_model: str | None = None   # the model that last succeeded; tried first next time


class AIUnavailable(Exception):
    """The AI could not answer. `kind` says why, so callers can respond honestly."""
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


def candidate_models() -> list[str]:
    ordered = [_working_model, CLASSIFIER_MODEL, *FALLBACK_MODELS]
    seen, out = set(), []
    for m in ordered:
        if m and m not in seen:
            seen.add(m); out.append(m)
    return out


def classify_error(exc: Exception) -> str:
    """One word for what went wrong. Order matters: the specific cases come first."""
    msg = str(exc).lower()
    if isinstance(exc, AuthenticationError) or isinstance(exc, PermissionDeniedError):
        return "auth"
    if "credit balance" in msg or "billing" in msg or "purchase credits" in msg:
        return "billing"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, NotFoundError):
        return "model"
    if isinstance(exc, APIConnectionError):
        return "connection"
    if isinstance(exc, APIStatusError) and (exc.status_code >= 500 or exc.status_code == 529):
        return "overloaded"
    if isinstance(exc, BadRequestError):
        return "bad_request"
    return "other"


def _client() -> Anthropic:
    return Anthropic(api_key=ANTHROPIC_API_KEY, timeout=25.0, max_retries=1)


def complete(system: str, messages: list[dict], max_tokens: int) -> str:
    """Ask the model and return its text. Raises AIUnavailable (never a raw SDK error)."""
    global _working_model
    if not ANTHROPIC_API_KEY:
        raise AIUnavailable("not_configured", "ANTHROPIC_API_KEY is not set")
    client = _client()
    last: AIUnavailable | None = None
    for model in candidate_models():
        try:
            resp = client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages)
        except Exception as exc:  # noqa: BLE001 — classified below, never leaked
            kind = classify_error(exc)
            status = getattr(exc, "status_code", None)
            log.error("AI call failed: model=%s kind=%s status=%s type=%s detail=%s",
                      model, kind, status, type(exc).__name__, str(exc)[:300])
            last = AIUnavailable(kind, str(exc)[:300])
            # A retired or unknown model, or a busy one, may well work on the next candidate.
            # A bad key, no credit or a rate limit would fail identically on every model.
            if kind in ("model", "overloaded"):
                continue
            raise last
        _working_model = model
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    raise last or AIUnavailable("model", "no model available")


def check() -> dict:
    """Used by /api/setup/ai-check: try each candidate with the smallest possible call."""
    key = ANTHROPIC_API_KEY or ""
    out: dict = {
        "key_present": bool(key),
        "key_looks_right": key.startswith("sk-ant-"),
        "key_hint": (f"sk-ant-…{key[-4:]}" if key.startswith("sk-ant-") else (f"…{key[-4:]}" if key else None)),
        "configured_model": CLASSIFIER_MODEL, "models": [],
    }
    if not key:
        out["verdict"] = "ANTHROPIC_API_KEY is not set on the server."
        return out
    client = _client()
    first_ok = None
    for model in candidate_models():
        t0 = time.time()
        try:
            client.messages.create(model=model, max_tokens=5, messages=[{"role": "user", "content": "Say OK"}])
            out["models"].append({"model": model, "ok": True, "seconds": round(time.time() - t0, 1)})
            first_ok = first_ok or model
        except Exception as exc:  # noqa: BLE001
            out["models"].append({"model": model, "ok": False, "kind": classify_error(exc),
                                  "status": getattr(exc, "status_code", None), "detail": str(exc)[:240]})
            if classify_error(exc) in ("auth", "billing", "rate_limit"):
                break   # identical on every model, so no point asking the rest
    kinds = {m.get("kind") for m in out["models"] if not m["ok"]}
    if first_ok:
        out["verdict"] = f"Working. Using {first_ok}." + (" The configured model did not work, so a fallback is being used: update CLASSIFIER_MODEL." if first_ok != CLASSIFIER_MODEL else "")
    elif "auth" in kinds:
        out["verdict"] = "The API key was rejected. Create a new key in the Anthropic console and update ANTHROPIC_API_KEY."
    elif "billing" in kinds:
        out["verdict"] = "The Anthropic account has no usable credit. Add credit in the Anthropic console (Plans and billing)."
    elif "rate_limit" in kinds:
        out["verdict"] = "The account is being rate limited. Wait a few minutes, or raise the limit in the Anthropic console."
    elif "model" in kinds:
        out["verdict"] = "None of the model names were accepted for this account. See the details per model."
    else:
        out["verdict"] = "The calls failed for another reason. See the details per model."
    return out
