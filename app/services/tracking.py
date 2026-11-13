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

from ..models import TrackingConversion, TrackingTag
from .promos import clean_tag

SCOPES = ("marketing", "app", "both")
_G_ID = re.compile(r"^(G-[A-Z0-9]{6,14}|AW-\d{6,14}|GT-[A-Z0-9]{4,14}|GTM-[A-Z0-9]{4,10})$")
# A Google Ads conversion label: the part after the slash in send_to "AW-123456789/AbCdEfGh123". Letters, digits, _ and - only.
_GADS_LABEL = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
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


# ---------------------------------------------------------------- conversions, with no deployment
# WHEN: a moment the app reports, or a page. The app reports these moments today; a moment it does not know yet can still be saved
# (so a later app release that starts reporting it needs no backend change), it simply never fires until then.
MOMENTS: list[dict] = [
    {"key": "trial_signup", "name": "Free trial account created"},
    {"key": "purchase", "name": "Payment confirmed"},
    {"key": "login", "name": "Signed in"},
]
_MOMENT = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_PAGE = re.compile(r"^page:/[A-Za-z0-9/_.\-]{0,100}\*?$")
_EVENT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,39}$")
MAX_CONVERSIONS_PER_TAG = 30

# WHAT: per platform, what a conversion reference looks like and where to find it. A platform not listed takes no conversions.
CONVERSIONS: dict[str, dict] = {
    "google_tag": {"what": "Conversion label", "pattern": _GADS_LABEL, "example": "cVOXCJCfj7cdEJjfyJg_", "needs_prefix": "AW-",
                   "where": "Google Ads, Goals, Conversions, open the conversion, Tag setup, Install the tag yourself: in send_to: 'AW-123456789/LABEL' copy only the "
                            "part after the slash. Only a Google Ads tag (AW-) takes conversion labels."},
    "meta_pixel": {"what": "Event name", "pattern": _EVENT_NAME, "example": "Lead",
                   "where": "A Meta standard event (Lead, CompleteRegistration, StartTrial, Subscribe, Purchase) or your own custom event name."},
    "linkedin_insight": {"what": "Conversion ID", "pattern": re.compile(r"^\d{5,12}$"), "example": "12345678",
                         "where": "LinkedIn Campaign Manager, Analyze, Conversion tracking, open the conversion: the ID in its event-specific snippet."},
    "x_pixel": {"what": "Event ID", "pattern": re.compile(r"^tw-[a-z0-9]+-[a-z0-9]+$"), "example": "tw-o1abc-o1def",
                "where": "X Ads, Tools, Events manager, open the event: the event ID (tw-…)."},
    "tiktok_pixel": {"what": "Event name", "pattern": _EVENT_NAME, "example": "CompleteRegistration",
                     "where": "A TikTok standard event (CompleteRegistration, SubmitForm, Subscribe, CompletePayment) or your own event name."},
}


def clean_trigger(value) -> str:
    v = str(value or "").strip()
    if v.startswith("page:"):
        if not _PAGE.match(v):
            raise TrackingError("A page must look like page:/dashboard (add * at the end to include every page under it, for example page:/reports*).")
        return v
    v = v.lower()
    if not _MOMENT.match(v):
        raise TrackingError("Choose when it fires: a moment such as trial_signup, or a page such as page:/dashboard.")
    return v


def validate_conversion(tag: TrackingTag, trigger, value) -> tuple[str, str]:
    """(clean trigger, clean value), or TrackingError saying what a good one looks like."""
    spec = CONVERSIONS.get(tag.provider)
    if not spec:
        raise TrackingError(f"{PROVIDERS.get(tag.provider, {}).get('name', tag.provider)} does not take conversions here. Use the platform's own events manager, or Google Tag Manager.")
    if spec.get("needs_prefix") and not str(tag.tag_id).startswith(spec["needs_prefix"]):
        raise TrackingError(f"Conversions go on the Google Ads tag ({spec['needs_prefix']}…), not on {tag.tag_id}. Add the Google Ads tag first.")
    trig = clean_trigger(trigger)
    v = str(value or "").strip()
    if tag.provider == "x_pixel":
        v = v.lower()
    if not spec["pattern"].match(v):
        raise TrackingError(f"That does not look like a {spec['what'].lower()}. It should look like {spec['example']}. {spec['where']}")
    return trig, v


def conversion_dict(c: TrackingConversion) -> dict:
    return {"id": c.id, "name": c.name, "trigger": c.trigger, "value": c.value, "enabled": bool(c.enabled),
            "updated_at": c.updated_at.isoformat() if c.updated_at else None, "updated_by": c.updated_by}


def conversions_of(db: Session, tag_pk: str) -> list[TrackingConversion]:
    return list(db.scalars(select(TrackingConversion).where(TrackingConversion.tag_pk == tag_pk).order_by(TrackingConversion.created_at)).all())


def _events_of(t: TrackingTag) -> dict:
    try:
        return json.loads(t.events) if t.events else {}
    except ValueError:
        return {}


def tag_dict(t: TrackingTag, db: Session | None = None) -> dict:
    spec = PROVIDERS.get(t.provider, {})
    convs = [conversion_dict(c) for c in conversions_of(db, t.id)] if db is not None else []
    return {"id": t.id, "provider": t.provider, "provider_name": spec.get("name", t.provider), "label": t.label, "tag_id": t.tag_id, "scope": t.scope,
            "category": category_for(t.provider, t.tag_id), "enabled": bool(t.enabled), "events": _events_of(t),
            "updated_at": t.updated_at.isoformat() if t.updated_at else None, "updated_by": t.updated_by, "conversions": convs}


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
        try:
            _, _, events = validate(t.provider, clean, _events_of(t))   # conversion IDs are checked again too; a bad one is dropped, the tag itself still loads
        except TrackingError:
            events = {}
        conversions = []
        for c in conversions_of(db, t.id):
            if not c.enabled:
                continue
            try:                                                                  # checked again, so a row edited by hand is never published
                trig, val = validate_conversion(TrackingTag(provider=t.provider, tag_id=clean), c.trigger, c.value)
            except TrackingError:
                continue
            conversions.append({"on": trig, "value": val})
        tags.append({"provider": t.provider, "id": clean, "category": category, "events": events, "conversions": conversions})
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
