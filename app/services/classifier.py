"""Claude-powered classification. One paid API, no approval process, cheap at
MVP volume — this is the only external dependency the intelligence layer
actually needs to work end to end."""
from __future__ import annotations
import json
import logging
from ..config import ANTHROPIC_API_KEY
from . import ai

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

Respond ONLY with a compact JSON array (one object per input, same order, each object on a single line, no indentation, no prose; keep each rationale under 15 words):
{"idx": <int>, "severity": "HIGH|MEDIUM|WATCH", "sentiment": "Positive|Negative|Neutral",
 "tags": [...], "lang": "..", "rationale": "<one short sentence>"}"""


MAX_SPLIT_DEPTH = 2     # how many times an incomplete answer may be re-asked in smaller pieces (at most 7 calls for one batch)


def _tokens_for(n_items: int) -> int:
    """Room for the answer. A reply that hits this ceiling is cut off mid-sentence and then cannot be read, which once
    sent a whole batch to WATCH, so it is generous (a ceiling, not a charge) and grows with the batch."""
    return min(8000, 250 * n_items + 400)


def _read_items(text: str) -> list[dict]:
    """Every COMPLETE object in the reply's JSON array, in order. Copes with prose or code fences around the array, indentation,
    and a reply that was cut off part way (the objects before the cut are kept, the unfinished one is dropped)."""
    text = (text or "").strip()
    start = text.find("[")
    if start < 0:
        return []
    decoder, pos, found = json.JSONDecoder(), start + 1, []
    while True:
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] == "]":
            break
        try:
            obj, pos = decoder.raw_decode(text, pos)
        except ValueError:
            break                       # cut off here: keep what was complete
        if isinstance(obj, dict):
            found.append(obj)
    return found


def classify_batch(brand_name: str, brand_context: str, items: list[dict], _depth: int = 0) -> list[dict]:
    if not items:
        return []
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — classification skipped, everything defaults to WATCH")
        return [_fallback(i["idx"]) for i in items]

    payload = json.dumps([{"idx": i["idx"], "platform": i.get("platform", ""), "text": i["text"][:900]}
                         for i in items], ensure_ascii=False)
    try:
        text = ai.complete(SYSTEM, [{"role": "user", "content":
                f"Brand: {brand_name}\nContext: {brand_context}\n\nClassify:\n{payload}"}], _tokens_for(len(items)))
        # Normalise every idx to int on both sides of the lookup: a fast model can return "idx": "0" (a string), and a single
        # int/string mismatch would silently lose a correct classification. (A real, confirmed bug, not a hypothetical one.)
        by_idx: dict[int, dict] = {}
        for p in _read_items(text):
            if "idx" not in p:
                continue
            try:
                by_idx[int(p["idx"])] = p
            except (TypeError, ValueError):
                continue
        missing = [i for i in items if int(i["idx"]) not in by_idx]
        redone: dict[int, dict] = {}
        if missing and _depth < MAX_SPLIT_DEPTH:
            log.warning("Classifier reply was incomplete (%d of %d items read); asking again for the rest", len(items) - len(missing), len(items))
            if len(missing) == len(items) and len(items) > 1:      # nothing was readable: try each half on its own
                half = (len(items) + 1) // 2
                again = classify_batch(brand_name, brand_context, items[:half], _depth + 1) + classify_batch(brand_name, brand_context, items[half:], _depth + 1)
            else:
                again = classify_batch(brand_name, brand_context, missing, _depth + 1)
            redone = {int(r["idx"]): r for r in again}
        return [_normalise(by_idx[int(i["idx"])]) if int(i["idx"]) in by_idx else redone.get(int(i["idx"]), _fallback(i["idx"])) for i in items]
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
