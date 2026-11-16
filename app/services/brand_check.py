"""services/brand_check.py. The free Brand Exposure Check: a visitor gives a brand name, its website and a work email, and gets a real,
public snapshot of two things BrandsLens already watches for customers: recent news that names the brand, and registered look alike
website addresses that copy the brand's own address. Then it invites them to the full free trial.

HOW IT FITS
  * The website posts the request here; the check is QUEUED, not run in the web request, because the look alike search resolves
    thousands of possible addresses and takes a few minutes. The worker (the separate scanning service) picks up queued checks every
    few seconds. The page polls with the check's secret token and shows the news part first, then the websites when they are ready.
  * Only public information is looked at, and only sources BrandsLens really scans (news and the domain check). Nothing is said about
    any platform we do not watch.
  * Cost is bounded three ways: business emails only, per email / per company / per day limits (Super Admin, no redeploy), and a
    24 hour cache: the same brand and website checked again within a day reuses the first result and costs nothing.
  * Each check is also a lead: the email, the brand and the campaign tags are kept, listed in Super Admin and downloadable as CSV.
    Marketing emails go only to people who ticked the box. The results email is part of the service they asked for.
"""
from __future__ import annotations

import csv
import html
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import APP_URL, TRIAL_FROM, TRIAL_REPLY_TO
from ..models import BrandCheck, aware, now_utc
from . import trial
from .settings import get_setting, set_setting

log = logging.getLogger("brand_check")

K = {"enabled": "brand_check:enabled", "daily_cap": "brand_check:daily_cap",
     "per_email": "brand_check:per_email_day", "per_company": "brand_check:per_company_day"}
DEFAULTS = {"enabled": False, "daily_cap": 300, "per_email": 3, "per_company": 6}
BOUNDS = {"daily_cap": (0, 5000), "per_email": (1, 20), "per_company": (1, 100)}
CACHE_HOURS = 24
STALE_MINUTES = 15                 # a check still "running" after this was interrupted (the worker restarted): try once more
MAX_ATTEMPTS = 2
NEWS_DAYS = 30
SHOW_NEWS, SHOW_DOMAINS, KEEP_DOMAINS = 10, 10, 25
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


class BrandCheckError(Exception):
    """Something to tell the visitor, with the HTTP status that fits."""
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ settings (Super Admin, no redeploy)
def get_settings(db: Session) -> dict:
    out = {"enabled": bool(get_setting(db, K["enabled"], DEFAULTS["enabled"]))}
    for k, (lo, hi) in BOUNDS.items():
        out[k] = trial._int(get_setting(db, K[k], DEFAULTS[k]), DEFAULTS[k], lo, hi)
    return out


def save_settings(db: Session, actor: str, data: dict) -> dict:
    if "enabled" in data:
        set_setting(db, K["enabled"], bool(data["enabled"]), updated_by=actor)
    for k, (lo, hi) in BOUNDS.items():
        if k in data:
            set_setting(db, K[k], trial._int(data[k], DEFAULTS[k], lo, hi), updated_by=actor)
    return get_settings(db)


# ------------------------------------------------------------------ input
def normalise_website(value: str) -> str:
    """'https://www.Acme-Foods.com/shop?x=1' -> 'acme-foods.com'. Raises BrandCheckError when it is not a public web address."""
    v = (value or "").strip().lower()
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0].split("@")[-1].split(":")[0].strip(".")
    if v.startswith("www."):
        v = v[4:]
    try:
        v = v.encode("idna").decode("ascii")          # an international address becomes its standard form
    except UnicodeError:
        raise BrandCheckError("Enter your brand's website, for example yourbrand.com.")
    if not _DOMAIN.match(v) or _IP.match(v):
        raise BrandCheckError("Enter your brand's website, for example yourbrand.com.")
    return v


def clean_brand(value: str) -> str:
    b = trial.clean_line(value, 80)
    if len(b) < 2:
        raise BrandCheckError("Enter your brand name.")
    return b


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _count(db: Session, *where) -> int:
    return db.scalar(select(func.count()).select_from(BrandCheck).where(*where)) or 0


# ------------------------------------------------------------------ start a check
def start(db: Session, *, email: str, brand: str, website: str, consent: bool = False, utm: dict | None = None,
          session_id: str = "", ip_hash: str = "", now: datetime | None = None) -> BrandCheck:
    now = now or now_utc()
    s = get_settings(db)
    if not s["enabled"]:
        raise BrandCheckError("The free brand check is not available right now. You can still start a free trial.", 503)
    email = trial.normalise_email(email)
    ok, reason = trial.is_business_email(email)
    if not ok:
        raise BrandCheckError(reason.replace("start a free trial", "use the brand check").replace("for a trial", "for the brand check"))
    brand, website = clean_brand(brand), normalise_website(website)
    domain, today = trial.domain_of(email), _day_start(now)

    if _count(db, BrandCheck.created_at >= today) >= s["daily_cap"]:
        raise BrandCheckError("Today's free brand checks are all taken. Please try again tomorrow, or start a free trial now.", 429)
    if _count(db, BrandCheck.email == email, BrandCheck.created_at >= today) >= s["per_email"]:
        raise BrandCheckError("You've used today's free brand checks. Start a free trial to watch your brand every day.", 429)
    if _count(db, BrandCheck.email_domain == domain, BrandCheck.created_at >= today) >= s["per_company"]:
        raise BrandCheckError("Your company has used today's free brand checks. Start a free trial to watch your brand every day.", 429)

    row = BrandCheck(email=email, email_domain=domain, brand=brand, website=website, marketing_consent=bool(consent),
                     session_id=trial.clean_line(session_id, 64), ip_hash=ip_hash, created_at=now, **trial._utm(utm or {}))
    recent = db.scalars(select(BrandCheck).where(BrandCheck.website == website, func.lower(BrandCheck.brand) == brand.lower(),
                                                 BrandCheck.status == "done", BrandCheck.finished_at >= now - timedelta(hours=CACHE_HOURS))
                        .order_by(BrandCheck.finished_at.desc())).first()
    if recent:                                   # the same brand checked within a day: reuse it, no new scan, no new cost
        row.status, row.news_done, row.cached_from = "done", True, recent.id
        row.news, row.domains = list(recent.news or []), list(recent.domains or [])
        row.news_total, row.news_attention, row.domains_total = recent.news_total, recent.news_attention, recent.domains_total
        row.started_at = row.finished_at = now
    db.add(row)
    db.commit()
    if row.status == "done":
        _after_finish(db, row)
        db.commit()
    log.info("Brand check %s for %s (%s) by %s%s", row.id, brand, website, domain, " (reused)" if row.cached_from else "")
    return row


# ------------------------------------------------------------------ the two checks (module level, so tests replace them)
def fetch_news(brand: str, website: str) -> list[dict]:
    """Recent news naming the brand, from the same global news search the scanner uses. [] if it cannot be reached."""
    import httpx
    from ..collectors.news_collector import GDELT_URL, _ts
    try:
        r = httpx.get(GDELT_URL, params={"query": f'"{brand}"', "mode": "artlist", "format": "json", "maxrecords": 30,
                                         "timespan": f"{NEWS_DAYS}d"}, timeout=30)
        r.raise_for_status()
        arts = r.json().get("articles", [])
    except Exception:  # noqa: BLE001
        log.warning("Brand check news search failed for %s", brand)
        raise
    out = []
    for a in arts:
        if a.get("title"):
            posted = _ts(a.get("seendate"))
            out.append({"title": trial.clean_line(a["title"], 300), "url": str(a.get("url", ""))[:600],
                        "source": trial.clean_line(a.get("domain", ""), 120), "date": posted.date().isoformat() if posted else ""})
    return out


def find_domains(website: str) -> list[str]:
    """Registered look alike addresses for the website, from the same domain check the scanner uses (no history lookups, for speed)."""
    import dnstwist
    results = dnstwist.run(domain=website, registered=True, format="null", threads=40) or []
    seen, out = set(), []
    for r in results:
        cand = str(r.get("domain", "")).lower().strip(".")
        if cand and cand != website and cand not in seen and _DOMAIN.match(cand):
            seen.add(cand)
            out.append(cand)
    return out


def rate_news(brand: str, website: str, items: list[dict]) -> list[str]:
    """HIGH / MEDIUM / WATCH for each headline, with the same rating BrandsLens uses for customers. WATCH if the rating is unavailable."""
    from . import classifier
    if not items:
        return []
    try:
        rated = classifier.classify_batch(brand, f"Public news check for the brand {brand} ({website})",
                                          [{"idx": i, "platform": "News", "text": it["title"]} for i, it in enumerate(items)])
        by_idx = {int(r.get("idx", -1)): str(r.get("severity", "WATCH")).upper() for r in rated}
    except Exception:  # noqa: BLE001
        log.exception("Brand check rating failed for %s", brand)
        by_idx = {}
    return [by_idx.get(i, "WATCH") if by_idx.get(i, "WATCH") in ("HIGH", "MEDIUM", "WATCH") else "WATCH" for i in range(len(items))]


_RANK = {"HIGH": 0, "MEDIUM": 1, "WATCH": 2}


def run(db: Session, row: BrandCheck) -> BrandCheck:
    """Runs one check: news first (saved, so the page can show it), then the look alike websites. A part that fails is reported as
    unavailable, never as 'nothing found'."""
    try:
        news = fetch_news(row.brand, row.website)
        severities = rate_news(row.brand, row.website, news)
        for it, sev in zip(news, severities):
            it["severity"] = sev
        news.sort(key=lambda n: n.get("date", ""), reverse=True)            # newest first ...
        news.sort(key=lambda n: _RANK.get(n["severity"], 2))               # ... then the serious ones on top (the sort is stable)
        row.news, row.news_total = news, len(news)
        row.news_attention = sum(1 for n in news if n["severity"] in ("HIGH", "MEDIUM"))
    except Exception:  # noqa: BLE001
        row.news, row.news_total, row.news_attention = [{"unavailable": True}], 0, 0
    row.news_done = True
    db.commit()
    try:
        found = find_domains(row.website)
        row.domains, row.domains_total = [{"domain": d} for d in found[:KEEP_DOMAINS]], len(found)
    except Exception:  # noqa: BLE001
        log.exception("Brand check domain search failed for %s", row.website)
        row.domains, row.domains_total = [{"unavailable": True}], 0
    row.finished_at = now_utc()
    _after_finish(db, row)              # the email goes before the check is marked done, so the page never claims an email that was not sent
    row.status = "done"
    db.commit()
    return row


def _claim(db: Session, row_id: str, now: datetime) -> bool:
    """Marks a queued check as running, only if nobody else did first (two worker runs can overlap)."""
    res = db.execute(update(BrandCheck).where(BrandCheck.id == row_id, BrandCheck.status == "queued")
                     .values(status="running", started_at=now, attempts=BrandCheck.attempts + 1))
    db.commit()
    return res.rowcount == 1


def process_queue(db: Session, limit: int = 1, now: datetime | None = None) -> int:
    """Runs up to `limit` waiting checks, oldest first. Called by the worker every few seconds."""
    now = now or now_utc()
    for stuck in db.scalars(select(BrandCheck).where(BrandCheck.status == "running",
                                                     BrandCheck.started_at < now - timedelta(minutes=STALE_MINUTES))).all():
        if stuck.attempts < MAX_ATTEMPTS:
            stuck.status = "queued"
        else:
            stuck.status, stuck.finished_at = "failed", now
    db.commit()
    done = 0
    for row in db.scalars(select(BrandCheck).where(BrandCheck.status == "queued").order_by(BrandCheck.created_at).limit(limit)).all():
        if not _claim(db, row.id, now):
            continue
        db.refresh(row)
        try:
            run(db, row)
        except Exception:  # noqa: BLE001 - one bad check must never stop the queue
            log.exception("Brand check %s failed", row.id)
            db.rollback()
            row = db.get(BrandCheck, row.id)
            row.status, row.finished_at = "failed", now_utc()
            db.commit()
        done += 1
    return done


# ------------------------------------------------------------------ what the visitor sees
def trial_link(row: BrandCheck) -> str:
    # The original campaign is kept, and utm_content says the trial came through the brand check, so both show on the trial lead.
    q = {"email": row.email, "utm_source": row.utm_source or "brand-check", "utm_medium": row.utm_medium or "brand-check",
         "utm_campaign": row.utm_campaign or "brand-check", "utm_content": "brand-check"}
    return f"{APP_URL}/trial?{urlencode(q)}"


def queue_position(db: Session, row: BrandCheck) -> int:
    if row.status != "queued":
        return 0
    return _count(db, BrandCheck.status == "queued", BrandCheck.created_at < row.created_at) + 1


def public_view(db: Session, row: BrandCheck) -> dict:
    news_unavailable = any(n.get("unavailable") for n in (row.news or []))
    domains_unavailable = any(d.get("unavailable") for d in (row.domains or []))
    done = row.status == "done"
    return {
        "id": row.id, "status": row.status, "brand": row.brand, "website": row.website,
        "queue_position": queue_position(db, row),
        "news": {"ready": bool(row.news_done), "unavailable": news_unavailable, "days": NEWS_DAYS, "total": row.news_total,
                 "attention": row.news_attention, "items": [] if news_unavailable else list(row.news or [])[:SHOW_NEWS]},
        "domains": {"ready": done, "unavailable": domains_unavailable, "total": row.domains_total,
                    "items": [] if domains_unavailable else [d["domain"] for d in (row.domains or [])][:SHOW_DOMAINS]},
        "emailed": row.emailed_at is not None,
        "trial_url": trial_link(row),
    }


# ------------------------------------------------------------------ the results email, and the lead
def _after_finish(db: Session, row: BrandCheck) -> None:
    """Sends the results email (once) and adds a consenting lead to Mailchimp. The caller commits."""
    if row.emailed_at is None and send_results_email(row):
        row.emailed_at = now_utc()
    if row.marketing_consent:
        try:
            from . import mailer
            mailer.add_to_mailchimp(row.email)
        except Exception:  # noqa: BLE001
            log.exception("Mailchimp add failed for a brand check lead")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def build_email(row: BrandCheck) -> tuple[str, str]:
    e = html.escape
    brand = e(row.brand)
    parts = [f"<p>Hi there,</p><p>Here is your free BrandsLens check for <b>{brand}</b> ({e(row.website)}).</p>"]
    news_unavailable = any(n.get("unavailable") for n in (row.news or []))
    if news_unavailable:
        parts.append("<p><b>News:</b> the news search was busy, so this part could not run. A free trial checks it continuously.</p>")
    else:
        parts.append(f"<p><b>News in the last {NEWS_DAYS} days:</b> {_plural(row.news_total, 'story', 'stories')} named {brand}"
                     + (f", and {_plural(row.news_attention, 'needs', 'need')} attention." if row.news_attention else ".") + "</p>")
        items = [n for n in (row.news or []) if n.get("severity") in ("HIGH", "MEDIUM")][:5] or list(row.news or [])[:3]
        if items:
            parts.append("<ul>" + "".join(f"<li><b>{e(n.get('severity', 'WATCH'))}</b> &middot; <a href=\"{e(n.get('url', ''))}\">{e(n.get('title', ''))}</a>"
                                          f" <span style=\"color:#64748B\">({e(n.get('source', ''))})</span></li>" for n in items) + "</ul>")
    domains_unavailable = any(d.get("unavailable") for d in (row.domains or []))
    if domains_unavailable:
        parts.append("<p><b>Look alike websites:</b> this part could not run this time. A free trial checks it continuously.</p>")
    elif row.domains_total:
        names = [d["domain"] for d in (row.domains or [])][:5]
        parts.append(f"<p><b>Look alike websites:</b> {_plural(row.domains_total, 'registered address copies', 'registered addresses copy')} "
                     f"{e(row.website)}. Check whether any of them is used to imitate you:</p><ul>"
                     + "".join(f"<li>{e(d)}</li>" for d in names) + "</ul>")
    else:
        parts.append(f"<p><b>Look alike websites:</b> we found no registered addresses imitating {e(row.website)} today.</p>")
    parts.append("<p>This is a one time snapshot. A free trial watches your brand every day across every source BrandsLens covers, "
                 "rates each mention by how serious it is, and alerts your team when something needs attention.</p>"
                 f"<p><a href=\"{e(trial_link(row))}\" style=\"background:#F59E0B;color:#0B0F17;padding:10px 20px;border-radius:8px;"
                 "text-decoration:none;font-weight:700;display:inline-block\">Start your free trial</a></p>"
                 "<p>Pat from BrandsLens</p>"
                 "<p style=\"color:#94A3B8;font-size:12px\">You received this because a free brand check was requested with this "
                 "email address at brandslens.com. We will not email you again unless you start a trial or agreed to hear from us.</p>")
    return f"Your free BrandsLens check for {row.brand}", "".join(parts)


def send_results_email(row: BrandCheck) -> bool:
    from . import mailer
    subject, inner = build_email(row)
    return mailer.send_email(row.email, subject, mailer._wrap(inner), from_addr=TRIAL_FROM, reply_to=TRIAL_REPLY_TO)


# ------------------------------------------------------------------ Super Admin
def summary(db: Session, now: datetime | None = None) -> dict:
    now = now or now_utc()
    today = _day_start(now)
    return {"today": _count(db, BrandCheck.created_at >= today),
            "last_7_days": _count(db, BrandCheck.created_at >= today - timedelta(days=6)),
            "total": _count(db), "waiting": _count(db, BrandCheck.status.in_(["queued", "running"]))}


def _row_dict(row: BrandCheck) -> dict:
    return {"id": row.id, "created_at": aware(row.created_at).isoformat() if row.created_at else "", "email": row.email,
            "brand": row.brand, "website": row.website, "status": row.status, "news_total": row.news_total,
            "news_attention": row.news_attention, "domains_total": row.domains_total, "reused": bool(row.cached_from),
            "marketing_consent": row.marketing_consent, "utm_source": row.utm_source, "utm_campaign": row.utm_campaign}


def list_checks(db: Session, *, q: str = "", limit_: int = 50, offset: int = 0) -> dict:
    stmt = select(BrandCheck)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(BrandCheck.email).like(like) | func.lower(BrandCheck.brand).like(like) | BrandCheck.website.like(like))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(BrandCheck.created_at.desc()).limit(max(1, min(limit_, 200))).offset(max(0, offset))).all()
    return {"total": total, "items": [_row_dict(r) for r in rows]}


def export_csv(db: Session) -> str:
    buf = io.StringIO()
    cols = ["created_at", "email", "brand", "website", "status", "news_total", "news_attention", "domains_total", "reused",
            "marketing_consent", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in db.scalars(select(BrandCheck).order_by(BrandCheck.created_at.desc())).all():
        d = _row_dict(r) | {"utm_medium": r.utm_medium, "utm_term": r.utm_term, "utm_content": r.utm_content}
        w.writerow([trial._safe_cell(d[c]) for c in cols])
    return buf.getvalue()
