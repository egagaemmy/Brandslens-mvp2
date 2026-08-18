"""Claude-powered classification. One paid API, no approval process, cheap at
MVP volume — this is the only external dependency the intelligence layer
actually needs to work end to end."""
from __future__ import annotations
import json
import logging
from anthropic import Anthropic
from ..config import ANTHROPIC_API_KEY, CLASSIFIER_MODEL

log = logging.getLogger("classifier")

SYSTEM = """You triage media mentions for a brand reputation and fraud monitoring \
tool. Classify each item:

Severity:
- HIGH: fraud, scams, impersonation, phishing/look-alike domains, solicitation of \
payments or personal data (BVN, NIN, cards), coordinated attacks, verified crises.
- MEDIUM: premature disclosure of non-public info, misinformation gaining traction, \
credible negative claims from identifiable sources.
- WATCH: general sentiment, opinion, speculation, positive coverage. Most items are WATCH.

Tags (zero or more): FRAUD, DOMAIN RISK, DISCLOSURE RISK, STRATEGIC.
Sentiment: Positive, Negative, or Neutral from the brand's perspective.

Respond ONLY with a JSON array, one object per input, same order, no prose:
{"idx": <int>, "severity": "HIGH|MEDIUM|WATCH", "sentiment": "Positive|Negative|Neutral",
 "tags": [...], "lang": "..", "rationale": "<one short sentence>"}"""


def classify_batch(brand_name: str, brand_context: str, items: list[dict]) -> list[dict]:
    if not items:
        return []
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — classification skipped, everything defaults to WATCH")
        return [_fallback(i["idx"]) for i in items]

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    payload = json.dumps([{"idx": i["idx"], "platform": i.get("platform", ""), "text": i["text"][:900]}
                         for i in items], ensure_ascii=False)
    try:
        resp = client.messages.create(
            model=CLASSIFIER_MODEL, max_tokens=1500, system=SYSTEM,
            messages=[{"role": "user", "content":
                f"Brand: {brand_name}\nContext: {brand_context}\n\nClassify:\n{payload}"}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(text)
        # Normalise every idx to int on both sides of this lookup — Claude
        # (especially a fast model like Haiku) can return "idx": "0" as a
        # JSON string instead of a number when the prompt only describes the
        # schema in prose rather than showing a worked example. A single
        # int/string mismatch here silently loses the real classification and
        # falls back to WATCH for that item, even though the model answered
        # correctly. This was a real, confirmed bug, not a hypothetical one.
        by_idx = {}
        for p in parsed:
            if not isinstance(p, dict) or "idx" not in p:
                continue
            try:
                by_idx[int(p["idx"])] = p
            except (TypeError, ValueError):
                continue
        return [_normalise(by_idx.get(int(i["idx"]), _fallback(i["idx"]))) for i in items]
    except Exception:  # noqa: BLE001 — never let a classifier hiccup drop data
        log.exception("Classification failed; everything in this batch defaults to WATCH")
        return [_fallback(i["idx"]) for i in items]


def _fallback(idx: int) -> dict:
    return {"idx": idx, "severity": "WATCH", "sentiment": "Neutral", "tags": [], "lang": "en",
            "rationale": "Fallback — classifier unavailable or errored."}


def _normalise(p: dict) -> dict:
    sev = str(p.get("severity", "WATCH")).upper()
    if sev not in ("HIGH", "MEDIUM", "WATCH"):
        sev = "WATCH"
    sent = str(p.get("sentiment", "Neutral")).title()
    if sent not in ("Positive", "Negative", "Neutral"):
        sent = "Neutral"
    tags = [t for t in (p.get("tags") or []) if t in ("FRAUD", "DOMAIN RISK", "DISCLOSURE RISK", "STRATEGIC")]
    try:
        idx = int(p.get("idx"))
    except (TypeError, ValueError):
        idx = p.get("idx")  # last resort — better to pass through than crash
    return {"idx": idx, "severity": sev, "sentiment": sent, "tags": tags,
            "lang": str(p.get("lang", "en"))[:8], "rationale": str(p.get("rationale", ""))[:300]}
