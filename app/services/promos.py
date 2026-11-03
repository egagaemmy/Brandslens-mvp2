"""services/promos.py. The launch offer: the rules, the maths and the launch report, in one place.

THE OFFER (decided for the 24 November 2026 launch)
  * 15% off the FIRST year of an annual plan. Monthly and daily billing are not eligible.
  * One code per platform (PH15, HN15, PEERLIST15, IH15), so which platform converts can be read
    from the codes as well as from the tracking links.
  * A code can have a start date, an end date and a redemption limit. Only PAID signups count towards
    the limit, so abandoned checkouts can never use up an offer.

HOW THE DISCOUNT IS CHARGED
  A normal annual plan is a recurring Flutterwave subscription whose amount is fixed by the payment plan
  (it cannot be lowered for one customer). A discounted first year is therefore charged as a ONE-TIME
  prepaid year at the reduced price, the same way the pay-in-naira route already works. When that year
  ends, the customer renews at full price through the normal renewal flow.

  If someone buys several years at once, only the first year is discounted:
      total = price * years - price * percent / 100
"""
from __future__ import annotations
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import LaunchEvent, PromoCode, SignupAttribution, aware, now_utc
from .settings import get_setting, set_setting

CODE_RE = re.compile(r"^[A-Z0-9]{3,24}$")
EVENT_KINDS = ("visit", "demo_click", "demo_start", "cta_click")
_TAG_RE = re.compile(r"[^a-z0-9._-]")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")


class PromoError(Exception):
    """A code that cannot be used. The message is written for the customer to read."""


def normalise(code, limit: int | None = 24) -> str:
    """Codes are typed by people: ignore case, spaces and punctuation. A customer's too-long guess is simply cut (it can
    never match); pass limit=None when CREATING a code, so a too-long code is refused instead of silently shortened."""
    out = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    return out[:limit] if limit else out


def clean_tag(value, limit: int = 60) -> str:
    """Campaign tags (utm_source and friends) are lower-case letters, digits and . _ - only, so nothing odd
    can be stored from a hand-edited link."""
    return _TAG_RE.sub("", str(value or "").strip().lower())[:limit]


def redemptions(db: Session, promo: PromoCode) -> int:
    """How many PAID signups used this code."""
    rows = db.scalars(select(SignupAttribution.id).where(SignupAttribution.promo_code == promo.code,
                                                         SignupAttribution.status == "paid")).all()
    return len(rows)


def get_valid(db: Session, code, plan: str, cycle: str, now: datetime | None = None) -> PromoCode:
    """The code, if it can be used right now for this purchase. Otherwise a PromoError with a plain reason."""
    code = normalise(code)
    if not code:
        raise PromoError("Enter a code.")
    promo = db.scalar(select(PromoCode).where(PromoCode.code == code))
    if not promo or not promo.active:
        raise PromoError("That code isn't valid.")
    now = now or now_utc()
    if promo.valid_from and aware(promo.valid_from) > now:
        raise PromoError("That offer hasn't started yet.")
    if promo.valid_until and aware(promo.valid_until) < now:
        raise PromoError("That offer has ended.")
    if cycle != "annual":
        raise PromoError("This offer applies to annual plans only. Choose annual billing to use it.")
    if promo.max_redemptions is not None and redemptions(db, promo) >= promo.max_redemptions:
        raise PromoError("That offer has been fully redeemed.")
    return promo


def price_with_promo(base_price: float, quantity: int, percent: int) -> tuple[float, float]:
    """(discount, total to charge). The discount applies to ONE year's price only, however many years are bought."""
    discount = round(base_price * percent / 100.0, 2)
    return discount, round(base_price * quantity - discount, 2)


def quote(db: Session, code, plan: str, cycle: str, quantity: int, base_price: float) -> dict:
    """What the customer will see before paying. Raises PromoError for an unusable code."""
    promo = get_valid(db, code, plan, cycle)
    discount, total = price_with_promo(base_price, quantity, promo.percent)
    return {"valid": True, "code": promo.code, "percent": promo.percent, "platform": promo.platform,
            "discount": discount, "total": total, "full_total": round(base_price * quantity, 2),
            "message": f"{promo.percent}% off your first year."}


# ---------------------------------------------------------------- attribution (where customers came from)

def record_started(db: Session, pending_id: str, email: str, plan: str, cycle: str, utm: dict, session_id: str,
                   promo: PromoCode | None, discount: float, charged: float, currency: str) -> SignupAttribution:
    row = SignupAttribution(
        pending_signup_id=pending_id, email=(email or "").lower()[:320],
        utm_source=clean_tag(utm.get("source")), utm_medium=clean_tag(utm.get("medium")),
        utm_campaign=clean_tag(utm.get("campaign"), 80), utm_term=clean_tag(utm.get("term"), 80), utm_content=clean_tag(utm.get("content"), 80),
        session_id=session_id if _SESSION_RE.match(session_id or "") else "",
        plan=plan, cycle=cycle, promo_code=promo.code if promo else "", discount_percent=promo.percent if promo else 0,
        discount_amount=discount, charged_amount=charged, currency=currency, status="started")
    db.add(row)
    return row


def mark_paid(db: Session, pending_id: str, org_id: str, charged: float | None = None, currency: str | None = None) -> bool:
    """Called when the payment webhook has proved the payment. Safe to call twice."""
    row = db.scalar(select(SignupAttribution).where(SignupAttribution.pending_signup_id == pending_id))
    if not row:
        return False
    row.organization_id = org_id or row.organization_id
    row.status = "paid"
    row.paid_at = row.paid_at or now_utc()
    if charged is not None:
        row.charged_amount = float(charged)
    if currency:
        row.currency = currency
    db.commit()
    return True


def mark_org_paid(db: Session, org_id: str, plan: str, cycle: str, charged: float | None = None, currency: str | None = None) -> bool:
    """The signed-in counterpart of mark_paid, for an existing account (for example a free-trial user) paying with an offer code.
    Matches the newest 'started' record for this organisation, plan and cycle, and ONLY if the amount really paid equals the discounted
    amount that was quoted, so paying full price some other way can never be counted as a redemption of the code."""
    if not org_id:
        return False
    row = db.scalar(select(SignupAttribution).where(SignupAttribution.organization_id == org_id, SignupAttribution.status == "started",
                                                    SignupAttribution.plan == plan, SignupAttribution.cycle == cycle,
                                                    SignupAttribution.promo_code != "").order_by(SignupAttribution.created_at.desc()))
    if not row:
        return False
    if charged is not None and abs(float(charged) - float(row.charged_amount)) > 0.01:
        return False
    row.status, row.paid_at = "paid", row.paid_at or now_utc()
    if charged is not None:
        row.charged_amount = float(charged)
    if currency:
        row.currency = currency
    db.commit()
    return True


# ---------------------------------------------------------------- the visit tracker

def clean_event(kind, session_id, source, medium, campaign, path, term="", content="") -> dict | None:
    """A validated event, or None if it is not one we record. Never stores anything that was not matched strictly."""
    if kind not in EVENT_KINDS or not _SESSION_RE.match(str(session_id or "")):
        return None
    path = str(path or "")
    path = path.split("?")[0].split("#")[0][:120]
    if path and not path.startswith("/"):
        return None
    return {"kind": kind, "session_id": session_id, "utm_source": clean_tag(source), "utm_medium": clean_tag(medium),
            "utm_campaign": clean_tag(campaign, 80), "utm_term": clean_tag(term, 80), "utm_content": clean_tag(content, 80), "path": path}


def record_event(db: Session, ev: dict) -> None:
    db.add(LaunchEvent(**ev))
    db.commit()


# ---------------------------------------------------------------- the launch report

def launch_summary(db: Session, since: datetime | None = None) -> dict:
    """Funnel per platform: visitors, demo clicks, demo starts, checkouts started, paid signups and revenue.

    "Visitors" counts distinct anonymous sessions that opened the launch page; everything else is a count of
    events. Revenue is kept per currency, never added across currencies."""
    q = select(LaunchEvent)
    if since:
        q = q.where(LaunchEvent.created_at >= since)
    events = db.scalars(q.limit(300_000)).all()
    aq = select(SignupAttribution)
    if since:
        aq = aq.where(SignupAttribution.created_at >= since)
    attrs = db.scalars(aq).all()

    src: dict[str, dict] = defaultdict(lambda: {"visitors": set(), "demo_clicks": 0, "demo_starts": 0, "cta_clicks": 0,
                                                "checkouts": 0, "paid": 0, "revenue": defaultdict(float)})
    for e in events:
        s = src[e.utm_source or "(direct)"]
        if e.kind == "visit":
            s["visitors"].add(e.session_id)
        elif e.kind == "demo_click":
            s["demo_clicks"] += 1
        elif e.kind == "demo_start":
            s["demo_starts"] += 1
        elif e.kind == "cta_click":
            s["cta_clicks"] += 1
    codes: dict[str, dict] = defaultdict(lambda: {"checkouts": 0, "paid": 0, "discount": defaultdict(float), "revenue": defaultdict(float)})
    for a in attrs:
        s = src[a.utm_source or "(direct)"]
        s["checkouts"] += 1
        if a.status == "paid":
            s["paid"] += 1
            s["revenue"][a.currency] += a.charged_amount
        if a.promo_code:
            c = codes[a.promo_code]
            c["checkouts"] += 1
            if a.status == "paid":
                c["paid"] += 1
                c["discount"][a.currency] += a.discount_amount
                c["revenue"][a.currency] += a.charged_amount
    sources = []
    for name, s in src.items():
        v = len(s["visitors"])
        sources.append({"source": name, "visitors": v, "demo_clicks": s["demo_clicks"], "demo_starts": s["demo_starts"],
                        "cta_clicks": s["cta_clicks"], "checkouts_started": s["checkouts"], "paid": s["paid"],
                        "revenue": {k: round(x, 2) for k, x in s["revenue"].items()},
                        "visit_to_paid_percent": round(100.0 * s["paid"] / v, 1) if v else None})
    sources.sort(key=lambda r: (-r["paid"], -r["visitors"], r["source"]))
    # The same funnel by exact campaign link (source, medium and campaign together), so two links from one platform can be compared.
    camp: dict[tuple, dict] = defaultdict(lambda: {"visitors": set(), "demo_starts": 0, "checkouts": 0, "paid": 0, "revenue": defaultdict(float)})
    for e in events:
        c = camp[(e.utm_source or "(direct)", e.utm_medium or "", e.utm_campaign or "")]
        if e.kind == "visit": c["visitors"].add(e.session_id)
        elif e.kind == "demo_start": c["demo_starts"] += 1
    for a in attrs:
        c = camp[(a.utm_source or "(direct)", a.utm_medium or "", a.utm_campaign or "")]; c["checkouts"] += 1
        if a.status == "paid": c["paid"] += 1; c["revenue"][a.currency] += a.charged_amount
    campaigns = sorted(({"source": k[0], "medium": k[1], "campaign": k[2], "visitors": len(v["visitors"]), "demo_starts": v["demo_starts"], "checkouts_started": v["checkouts"],
                         "paid": v["paid"], "revenue": {x: round(y, 2) for x, y in v["revenue"].items()}} for k, v in camp.items()),
                       key=lambda r: (-r["paid"], -r["visitors"], r["source"], r["campaign"]))
    out_codes = [{"code": k, "checkouts_started": c["checkouts"], "paid": c["paid"],
                  "discount_given": {x: round(y, 2) for x, y in c["discount"].items()},
                  "revenue": {x: round(y, 2) for x, y in c["revenue"].items()}} for k, c in sorted(codes.items())]
    return {"sources": sources, "campaigns": campaigns, "codes": out_codes,
            "totals": {"visitors": sum(r["visitors"] for r in sources), "demo_starts": sum(r["demo_starts"] for r in sources),
                       "checkouts_started": sum(r["checkouts_started"] for r in sources), "paid": sum(r["paid"] for r in sources)}}


# ---------------------------------------------------------------- the launch codes

LAGOS_OFFSET = timedelta(hours=1)  # Africa/Lagos is UTC+1 all year, no daylight saving
DEFAULT_LAUNCH_CODES = [("PH15", "Product Hunt"), ("HN15", "Hacker News"), ("PEERLIST15", "Peerlist"), ("IH15", "Indie Hackers")]


def seed_launch_codes(db: Session, percent: int = 15, starts_on: str = "2026-11-24", days: int = 31,
                      codes: list[tuple[str, str]] | None = None) -> dict:
    """Create the launch codes if they do not exist yet (safe to run twice; it never overwrites a code you edited).
    A code runs from midnight Lagos time on its start date, for the given number of days."""
    y, m, d = (int(x) for x in starts_on.split("-"))
    start = datetime(y, m, d, tzinfo=timezone.utc) - LAGOS_OFFSET
    end = start + timedelta(days=days)
    created, kept = [], []
    for code, platform in (codes or DEFAULT_LAUNCH_CODES):
        code = normalise(code)
        if db.scalar(select(PromoCode).where(PromoCode.code == code)):
            kept.append(code)
            continue
        db.add(PromoCode(code=code, percent=percent, platform=platform, valid_from=start, valid_until=end, active=True))
        created.append(code)
    db.commit()
    return {"created": created, "already_existed": kept, "percent": percent,
            "valid_from_utc": start.isoformat(), "valid_until_utc": end.isoformat()}


# ---------------------------------------------------------------- the standing first-purchase offer
# Independent of any launch. One ordinary offer code (for example FIRST15) is named in the settings, and from then on it is applied BY ITSELF to
# every customer's first purchase, with the same rules as any code: a percentage off the first year of an annual plan, then full price at renewal.
# Because it is an ordinary code, its dates, its cap, its on/off switch and its redemptions all work as they do for every other code, and it appears
# in the launch report like one. The server decides: the screens only show what the server will charge.
STANDING_KEY = "promo:standing_code"


def standing_code(db: Session) -> str:
    """The code applied automatically to every first purchase. Empty means there is no standing offer."""
    return normalise(get_setting(db, STANDING_KEY, "") or "", limit=None)


def set_standing_code(db: Session, code: str, by: str = "") -> None:
    set_setting(db, STANDING_KEY, normalise(code, limit=None) if code else "", by)


def has_paid_before(db: Session, org) -> bool:
    """True if this organisation has ever really paid (or is a team account). A free trial or an account that never paid has not."""
    if org.billing_status in ("active", "past_due", "cancelled", "exempt"):
        return True
    if org.plan_activated_at or org.paid_until or org.billing_subscription_id or org.billing_customer_id or org.billing_provider or org.billing_cycle:
        return True
    return bool(db.scalar(select(SignupAttribution.id).where(SignupAttribution.organization_id == org.id, SignupAttribution.status == "paid")))


def standing_promo(db: Session, plan: str, cycle: str, *, first_purchase: bool, now: datetime | None = None) -> PromoCode | None:
    """The standing offer, if this purchase qualifies: a first purchase, an annual plan, and the code is live. Never raises."""
    if not first_purchase:
        return None
    code = standing_code(db)
    if not code:
        return None
    try:
        return get_valid(db, code, plan, cycle, now)
    except PromoError:
        return None


def choose_offer(typed: PromoCode | None, standing: PromoCode | None) -> PromoCode | None:
    """One offer per purchase, never two added together: the better of a typed code and the standing offer. On a tie the typed code wins,
    so a platform code such as PH15 still shows up against its platform in the launch report."""
    if typed and standing:
        return standing if standing.percent > typed.percent else typed
    return typed or standing


def standing_status(db: Session, now: datetime | None = None) -> dict:
    """What anyone may be told about the offer: is it on right now, and for how much."""
    code = standing_code(db)
    if not code:
        return {"active": False}
    try:
        promo = get_valid(db, code, "growth", "annual", now)
    except PromoError:
        return {"active": False}
    return {"active": True, "code": promo.code, "percent": promo.percent, "message": f"{promo.percent}% off your first year, applied automatically."}


def standing_for_org(db: Session, org) -> dict | None:
    """The offer as it applies to a signed-in organisation: None once they have paid before (or are a team account)."""
    if has_paid_before(db, org):
        return None
    status = standing_status(db)
    return {k: status[k] for k in ("code", "percent", "message")} if status["active"] else None

