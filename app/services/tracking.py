"""services/tracking.py. Ad and analytics tags managed from Super Admin, and the UTM link builder.

WHY ONLY IDs
  Each tag is stored as the platform's own ID (a Meta Pixel ID, a Google tag ID and so on), checked against that platform's ID
  format. No script code is ever stored or sent to visitors: the websites already contain the standard install code for each
  platform and only fill in the ID. So a mistyped entry cannot break a page, and a stolen admin login cannot be used to push
  hostile code to every visitor. For anything not listed here, add it inside Google Tag Manager (its ID is supported).

CONSENT
  The category of each tag (analytics or marketing) is decided here from the provider and ID, NOT chosen by whoever adds the tag,
  so a marketing pixel can never be filed under a category that skips consent. The websites load a tag only after the visitor
  has agreed to its category.
"""
from __future__ import annotations
import json
import re
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import TrackingTag
from .promos import clean_tag

SCOPES = ("marketing", "app", "both")
_G_ID = re.compile(r"^(G-[A-Z0-9]{6,14}|AW-\d{6,14}|GT-[A-Z0-9]{4,14}|GTM-[A-Z0-9]{4,10})$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

PROVIDERS: dict[str, dict] = {
    "meta_pixel": {
        "name": "Meta Pixel (Facebook and Instagram)", "pattern": re.compile(r"^\d{10,20}$"), "example": "123456789012345",
        "where": "Meta Events Manager, Data sources, your pixel: the Pixel ID (digits only).", "events": {}},
    "google_tag": {
        "name": "Google tag (Analytics, Ads or Tag Manager)", "pattern": _G_ID, "example": "G-ABC123XYZ9",
        "where": "Google Analytics: Admin, Data streams, the Measurement ID (G-…). Google Ads: the tag ID (AW-…). Tag Manager: the container ID (GTM-…).", "events": {}},
    "linkedin_insight": {
        "name": "LinkedIn Insight Tag", "pattern": re.compile(r"^\d{5,12}$"), "example": "1234567",
        "where": "LinkedIn Campaign Manager, Analyze, Insight tag: the Partner ID (digits only).",
        "events": {"lead": re.compile(r"^\d{5,12}$"), "purchase": re.compile(r"^\d{5,12}$")}},
    "x_pixel": {
        "name": "X (Twitter) pixel", "pattern": re.compile(r"^[a-z0-9]{4,12}$"), "example": "o1abc",
        "where": "X Ads, Tools, Events manager: the pixel ID (letters and digits, for example o1abc).",
        "events": {"lead": re.compile(r"^tw-[a-z0-9]+-[a-z0-9]+$"), "purchase": re.compile(r"^tw-[a-z0-9]+-[a-z0-9]+$")}},
    "tiktok_pixel": {
        "name": "TikTok pixel", "pattern": re.compile(r"^[A-Z0-9]{10,24}$"), "example": "C1ABCDEFGHIJKLMNOPQR",
        "where": "TikTok Ads Manager, Events, Web events: the Pixel ID (capital letters and digits).", "events": {}},
    "pinterest_tag": {
        "name": "Pinterest tag", "pattern": re.compile(r"^\d{10,16}$"), "example": "2612345678901",
        "where": "Pinterest Ads, Conversions, your tag: the Tag ID (digits only).", "events": {}},
    "snap_pixel": {
        "name": "Snap Pixel", "pattern": _UUID, "example": "12345678-abcd-4abc-8abc-1234567890ab",
        "where": "Snap Ads Manager, Events manager: the Pixel ID (it looks like a long code with dashes).", "events": {}},
    "clarity": {
        "name": "Microsoft Clarity", "pattern": re.compile(r"^[a-z0-9]{8,12}$"), "example": "abcd1234ef",
        "where": "Microsoft Clarity, Settings, Overview: the Project ID (letters and digits).", "events": {}},
}


class TrackingError(Exception):
    """A tag or link that cannot be saved. The message is written for the person adding it."""


def normalise_id(provider: str, value) -> str:
    v = str(value or "").strip()
    if provider in ("google_tag", "tiktok_pixel"):
        return v.upper()
    if provider in ("x_pixel", "clarity", "snap_pixel"):
        return v.lower()
    return v


def category_for(provider: str, tag_id: str) -> str:
    """Analytics tags measure; marketing tags build audiences or report to ad platforms, so they need marketing consent.
    A Google Analytics ID (G-…) is analytics; Google Ads and Tag Manager (which can load anything) count as marketing."""
    if provider == "clarity" or (provider == "google_tag" and tag_id.startswith("G-")):
        return "analytics"
    return "marketing"


def validate(provider: str, tag_id, events: dict | None = None) -> tuple[str, str, dict]:
    """(clean id, category, clean events). Raises TrackingError with an example if the ID does not look right."""
    spec = PROVIDERS.get(provider)
    if not spec:
        raise TrackingError("Choose one of the supported platforms.")
    clean = normalise_id(provider, tag_id)
    if not spec["pattern"].match(clean):
        raise TrackingError(f"That does not look like a {spec['name']} ID. It should look like {spec['example']}. {spec['where']}")
    out: dict = {}
    for key, value in (events or {}).items():
        pat = spec["events"].get(key)
        if pat is None:
            raise TrackingError(f"{spec['name']} does not take a '{key}' conversion ID.")
        v = str(value or "").strip().lower() if provider == "x_pixel" else str(value or "").strip()
        if not v:
            continue
        if not pat.match(v):
            raise TrackingError(f"The '{key}' conversion ID for {spec['name']} does not look right.")
        out[key] = v
    return clean, category_for(provider, clean), out


def _events_of(t: TrackingTag) -> dict:
    try:
        return json.loads(t.events) if t.events else {}
    except ValueError:
        return {}


def tag_dict(t: TrackingTag) -> dict:
    spec = PROVIDERS.get(t.provider, {})
    return {"id": t.id, "provider": t.provider, "provider_name": spec.get("name", t.provider), "label": t.label, "tag_id": t.tag_id, "scope": t.scope,
            "category": category_for(t.provider, t.tag_id), "enabled": bool(t.enabled), "events": _events_of(t),
            "updated_at": t.updated_at.isoformat() if t.updated_at else None, "updated_by": t.updated_by}


def public_config(db: Session, scope: str) -> dict:
    """What a website needs to load its tags: nothing but the enabled tags for that site, their category, and their ID. No labels,
    no timestamps, nobody's email. Cached for a minute by the websites and any CDN."""
    wanted = ("marketing", "both") if scope == "marketing" else ("app", "both")
    rows = db.scalars(select(TrackingTag).where(TrackingTag.enabled == True, TrackingTag.scope.in_(wanted))      # noqa: E712
                      .order_by(TrackingTag.created_at)).all()
    tags = []
    for t in rows:
        if t.provider not in PROVIDERS:
            continue
        try:
            clean, category, _ = validate(t.provider, t.tag_id)      # checked again here: a row edited by hand in the database is never published if its ID is malformed
        except TrackingError:
            continue
        tags.append({"provider": t.provider, "id": clean, "category": category, "events": _events_of(t)})
    return {"consent_required": True, "tags": tags}


def clean_destination(value: str) -> str:
    """A page on a BrandsLens site: a path such as /launch, or a full https address on brandslens.com or one of its subdomains.
    No query string: the builder adds the campaign tags itself."""
    v = (value or "").strip()
    if not v or len(v) > 300 or any(c.isspace() for c in v):
        raise TrackingError("Enter the page address without spaces, for example /launch.")
    if v.startswith("/"):
        if "?" in v or "#" in v or v.startswith("//"):
            raise TrackingError("Enter the page address without a question mark or a #. The campaign tags are added for you.")
        return v
    parts = urlsplit(v)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "brandslens.com" or host.endswith(".brandslens.com")):
        raise TrackingError("Links must go to a BrandsLens page: a path like /launch, or an https address on brandslens.com.")
    if parts.query or parts.fragment:
        raise TrackingError("Enter the page address without a question mark or a #. The campaign tags are added for you.")
    return v


def build_utm_url(destination: str, source: str, medium: str = "", campaign: str = "", term: str = "", content: str = "",
                  base: str = "https://www.brandslens.com") -> str:
    dest = clean_destination(destination)
    full = dest if not dest.startswith("/") else base.rstrip("/") + dest
    params = [("utm_source", clean_tag(source)), ("utm_medium", clean_tag(medium)), ("utm_campaign", clean_tag(campaign, 80)),
              ("utm_term", clean_tag(term, 80)), ("utm_content", clean_tag(content, 80))]
    q = urlencode([(k, v) for k, v in params if v])
    return f"{full}?{q}" if q else full
