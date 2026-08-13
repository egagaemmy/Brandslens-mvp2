"""Look-alike domain & phishing watch — dnstwist + optional WhoisXML. Entirely
free, no API gatekeeper of any kind, and one of the strongest differentiators
against any generic social-listening tool."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
import httpx
from ..config import WHOISXML_API_KEY
from ..models import Workspace

log = logging.getLogger("collector.domains")

def collect(db, ws: Workspace) -> list[dict]:
    out: list[dict] = []
    for d in (ws.brand_domains or []):
        out += _dnstwist(d)
    for t in (ws.brand_tokens or []):
        out += _whoisxml_nrd(t)
    return out

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
        out.append({"text": f"Look-alike domain registered and resolving: {cand} "
                           f"(permutation of {domain}, fuzzer: {r.get('fuzzer','')}). "
                           f"Verify content for impersonation/phishing.",
                   "url": f"http://{cand}", "author": "dnstwist", "platform": "Domain Watch",
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
