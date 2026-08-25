"""Look-alike domain & phishing watch — dnstwist + optional WhoisXML, now
enriched with free Wayback Machine history for each detected domain. Entirely
free, no API gatekeeper of any kind, and one of the strongest differentiators
against any generic social-listening tool."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
import httpx
from ..config import WHOISXML_API_KEY
from ..models import Workspace

log = logging.getLogger("collector.domains")

def collect(db, ws: Workspace, days_back: int | None = None) -> list[dict]:
    out: list[dict] = []
    for d in (ws.brand_domains or []):
        out += _dnstwist(d)
    for t in (ws.brand_tokens or []):
        out += _whoisxml_nrd(t)
    return out

def _wayback_history(domain: str) -> str:
    """Checks the free, keyless Wayback Machine CDX API for this domain's
    earliest archived snapshot. A domain with NO history at all is a much
    stronger phishing signal than one that's existed for years — this is
    real context a look-alike domain match alone doesn't give you. Degrades
    silently on any failure — this is an enrichment, not something that
    should ever block the core look-alike detection itself."""
    try:
        r = httpx.get("http://web.archive.org/cdx/search/cdx",
                      params={"url": domain, "output": "json", "limit": 1}, timeout=15)
        r.raise_for_status()
        rows = r.json()
    except Exception:  # noqa: BLE001
        log.warning("Wayback Machine check failed for %s (non-fatal, skipping)", domain)
        return ""
    if not rows or len(rows) <= 1:  # first row is always the CDX header row
        return "No Wayback Machine history found — consistent with a brand-new registration."
    try:
        timestamp = rows[1][0]  # CDX format per row: [timestamp, original] — column 0 is the timestamp
        year, month, day = timestamp[:4], timestamp[4:6], timestamp[6:8]
        return f"First archived on the Wayback Machine: {year}-{month}-{day}."
    except (IndexError, TypeError):
        return ""

def _dnstwist(domain: str) -> list[dict]:
    try:
        import dnstwist  # heavy import, lazy — pip install dnstwist
        results = dnstwist.run(domain=domain, registered=True, format="null") or []
    except Exception:  # noqa: BLE001
        log.exception("dnstwist failed for %s", domain)
        return []
    out = []
    for r in results:
        cand = r.get("domain", "")
        if not cand or cand == domain:
            continue
        wayback_note = _wayback_history(cand)
        text = (f"Look-alike domain registered and resolving: {cand} "
               f"(permutation of {domain}, fuzzer: {r.get('fuzzer','')}). "
               f"Verify content for impersonation/phishing.")
        if wayback_note:
            text += f" {wayback_note}"
        out.append({"text": text, "url": f"http://{cand}", "author": "dnstwist", "platform": "Domain Watch",
                   "posted_at": datetime.now(timezone.utc), "reach": 0})
    return out

def _whoisxml_nrd(token: str) -> list[dict]:
    if not WHOISXML_API_KEY:
        return []
    try:
        r = httpx.get("https://newly-registered-domains.whoisxmlapi.com/api/v1",
                      params={"apiKey": WHOISXML_API_KEY, "searchTerm": token, "mode": "purchase"}, timeout=40)
        r.raise_for_status()
        domains = r.json().get("domainsList", [])[:40]
    except Exception:  # noqa: BLE001
        log.exception("WhoisXML NRD failed for %s", token)
        return []
    return [{"text": f"Newly registered domain contains brand token '{token}': {d}. Assess for phishing.",
            "url": f"http://{d}", "author": "whoisxml-nrd", "platform": "Domain Watch",
            "posted_at": datetime.now(timezone.utc), "reach": 0} for d in domains]
