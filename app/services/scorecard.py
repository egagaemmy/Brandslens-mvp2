"""app/services/scorecard.py: the weekly growth scorecard, emailed to the team every Monday morning (Lagos time).

It counts, straight from the database, what happened in the last full week (Monday 00:00 to Sunday 23:59, Lagos time):
free brand checks, trial requests, trials started and paid sign-ups, each split by the campaign tags the visitor arrived
with (utm_source and utm_medium). It also counts how many trials came from people who had run a brand check first, and
compares trials started since the plan began with the plan's weekly targets.

Every number is checked before the email goes out: each split by source must add up to its total, and each total is
counted twice in two different ways. Anything that does not agree is printed at the top of the email, so a wrong number
is never sent silently. Visits from our own team (brandslens.com addresses) are left out and reported on their own line.

Read only: nothing here changes any record except the note of which week has already been sent, which stops the same
week being emailed twice. Turn it off with SCORECARD_TO=off; send it elsewhere with SCORECARD_TO=a@x.com,b@y.com.
"""
from __future__ import annotations

import html
import logging
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import TRIAL_FROM, TRIAL_REPLY_TO
from ..models import BrandCheck, SignupAttribution, TrialSignup, aware, now_utc
from .settings import get_setting, set_setting

log = logging.getLogger("scorecard")

LAGOS = timezone(timedelta(hours=1))            # Lagos keeps UTC+1 all year (no daylight saving)
SEND_HOUR = 9                                    # Monday 09:00 Lagos or the first worker run after it
DEFAULT_TO = "brandslenslimited@gmail.com"
INTERNAL_DOMAINS = ("brandslens.com",)
SENT_KEY = "scorecard:last_sent_week"

# The go to market plan: weeks run Wednesday to Tuesday from 18 Nov 2026. Targets are trials started.
PLAN_START = date(2026, 11, 18)
PLAN_TARGETS = [150, 500, 800, 1050, 1150, 700, 650]


def recipients() -> list[str]:
    raw = os.environ.get("SCORECARD_TO", DEFAULT_TO).strip()
    if raw.lower() in ("", "off", "0", "false", "no"):
        return []
    return [a.strip() for a in raw.split(",") if re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", a.strip())]


def week_bounds(now: datetime) -> tuple[datetime, datetime, date]:
    """The last full Monday to Sunday week before `now`, as UTC datetimes [start, end), and its Monday in Lagos."""
    local = aware(now).astimezone(LAGOS)
    this_monday = (local - timedelta(days=local.weekday())).date()
    monday = this_monday - timedelta(days=7)
    start = datetime(monday.year, monday.month, monday.day, tzinfo=LAGOS)
    return start.astimezone(timezone.utc), (start + timedelta(days=7)).astimezone(timezone.utc), monday


def _internal(email: str) -> bool:
    dom = (email or "").rsplit("@", 1)[-1].lower()
    return any(dom == d or dom.endswith("." + d) for d in INTERNAL_DOMAINS)


def _channel(source: str, medium: str) -> str:
    s, m = (source or "").strip().lower(), (medium or "").strip().lower()
    if not s:
        return "untagged"
    return f"{s} / {m}" if m else s


def _rows_in(db: Session, model, col, start: datetime, end: datetime) -> list:
    return db.scalars(select(model).where(col.isnot(None), col >= start, col < end)).all()


def _count_in(db: Session, model, col, start: datetime, end: datetime, extra=None) -> int:
    q = select(func.count()).select_from(model).where(col.isnot(None), col >= start, col < end)
    if extra is not None:
        q = q.where(extra)
    return int(db.scalar(q) or 0)


def _split(rows, email_attr: str) -> tuple[list, int]:
    """Our own team's rows are left out of the figures and counted separately."""
    keep = [r for r in rows if not _internal(getattr(r, email_attr, ""))]
    return keep, len(rows) - len(keep)


def plan_position(day: date) -> tuple[int, int]:
    """(plan week number for `day`, cumulative target of plan weeks fully finished by the end of `day`)."""
    if day < PLAN_START:
        return 0, 0
    week = (day - PLAN_START).days // 7 + 1
    finished = min(len(PLAN_TARGETS), ((day - PLAN_START).days + 1) // 7)
    return week, sum(PLAN_TARGETS[:finished])


def build(db: Session, now: datetime | None = None) -> dict:
    now = now or now_utc()
    start, end, monday = week_bounds(now)
    problems: list[str] = []

    # Brand checks requested in the week
    bc_all = _rows_in(db, BrandCheck, BrandCheck.created_at, start, end)
    bc, bc_team = _split(bc_all, "email")
    bc_by = Counter(_channel(r.utm_source, r.utm_medium) for r in bc)
    bc_status = Counter(r.status for r in bc)
    bc_emailed = sum(1 for r in bc if r.emailed_at is not None)
    bc_reused = sum(1 for r in bc if r.cached_from)

    # Trials: requested (created) and started (verified and running) in the week
    tr_req_all = _rows_in(db, TrialSignup, TrialSignup.created_at, start, end)
    tr_req, _ = _split(tr_req_all, "email")
    tr_all = _rows_in(db, TrialSignup, TrialSignup.started_at, start, end)
    tr, tr_team = _split(tr_all, "email")
    tr_by = Counter(_channel(r.utm_source, r.utm_medium) for r in tr)

    # Paid sign-ups in the week
    paid_all = db.scalars(select(SignupAttribution).where(SignupAttribution.status == "paid", SignupAttribution.paid_at.isnot(None),
                                                          SignupAttribution.paid_at >= start, SignupAttribution.paid_at < end)).all()
    paid, _ = _split(paid_all, "email")
    paid_by = Counter(_channel(r.utm_source, r.utm_medium) for r in paid)

    # Trials started this week by someone who had run a brand check before starting
    checked = set()
    if tr:
        emails = {r.email.lower() for r in tr}
        for e, first in db.execute(select(func.lower(BrandCheck.email), func.min(BrandCheck.created_at))
                                   .where(func.lower(BrandCheck.email).in_(emails)).group_by(func.lower(BrandCheck.email))).all():
            checked.add((e, aware(first)))
    first_check = {e: t for e, t in checked}
    from_check = sum(1 for r in tr if r.email.lower() in first_check and first_check[r.email.lower()] <= aware(r.started_at))

    # Since the plan began
    plan_start_utc = datetime(PLAN_START.year, PLAN_START.month, PLAN_START.day, tzinfo=LAGOS).astimezone(timezone.utc)
    cum_rows, _ = _split(_rows_in(db, TrialSignup, TrialSignup.started_at, plan_start_utc, end), "email")
    last_day = (end.astimezone(LAGOS) - timedelta(days=1)).date()
    plan_week, cum_target = plan_position(last_day)

    # Accuracy checks: splits add up, and each total agrees with an independent database count
    for name, by, total in (("Brand checks", bc_by, len(bc)), ("Trials started", tr_by, len(tr)), ("Paid sign-ups", paid_by, len(paid))):
        if sum(by.values()) != total:
            problems.append(f"{name}: the split by source adds up to {sum(by.values())}, not {total}.")
    for name, rows, model, col in (("Brand checks", bc_all, BrandCheck, BrandCheck.created_at),
                                   ("Trial requests", tr_req_all, TrialSignup, TrialSignup.created_at),
                                   ("Trials started", tr_all, TrialSignup, TrialSignup.started_at)):
        recount = _count_in(db, model, col, start, end)
        if recount != len(rows):
            problems.append(f"{name}: counted {len(rows)} records but the database total is {recount}.")
    if sum(bc_status.values()) != len(bc):
        problems.append("Brand checks: the status counts do not add up to the total.")

    return {
        "week_start": monday.isoformat(), "week_end": (monday + timedelta(days=6)).isoformat(),
        "brand_checks": {"total": len(bc), "by_channel": dict(bc_by), "done": bc_status.get("done", 0), "failed": bc_status.get("failed", 0),
                         "waiting": bc_status.get("queued", 0) + bc_status.get("running", 0), "emailed": bc_emailed, "reused": bc_reused, "team": bc_team},
        "trial_requests": len(tr_req),
        "trials": {"total": len(tr), "by_channel": dict(tr_by), "from_brand_check": from_check, "team": tr_team},
        "paid": {"total": len(paid), "by_channel": dict(paid_by)},
        "plan": {"week": plan_week, "trials_since_start": len(cum_rows), "target_to_date": cum_target, "total_target": sum(PLAN_TARGETS)},
        "checks_passed": not problems, "problems": problems,
    }


def _pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "n/a"


def _fmt_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _table(title: str, by: dict) -> str:
    if not by:
        return f"<p style='margin:4px 0 14px;color:#475569'>{html.escape(title)}: none this week.</p>"
    rows = "".join(f"<tr><td style='padding:4px 12px 4px 0'>{html.escape(k)}</td><td style='padding:4px 0;text-align:right'><b>{v}</b></td></tr>"
                   for k, v in sorted(by.items(), key=lambda kv: (-kv[1], kv[0])))
    return (f"<p style='margin:14px 0 4px'><b>{html.escape(title)}</b></p>"
            f"<table style='border-collapse:collapse;font-size:14px'>{rows}</table>")


def render(card: dict) -> tuple[str, str]:
    bc, tr, paid, plan = card["brand_checks"], card["trials"], card["paid"], card["plan"]
    period = f"{_fmt_day(card['week_start'])} to {_fmt_day(card['week_end'])}"
    subject = f"BrandsLens weekly scorecard: {period}"
    if card["problems"]:
        check = ("<div style='background:#FEE2E2;color:#991B1B;padding:12px 14px;border-radius:8px;margin-bottom:16px'><b>Please read before "
                 "using these numbers.</b> Some figures did not agree when checked:<br>" + "<br>".join(html.escape(p) for p in card["problems"]) + "</div>")
    else:
        check = ("<p style='color:#047857;margin:0 0 16px'>All figures were checked: every split adds up to its total, and every total "
                 "matched a second count in the database.</p>")
    best = max(tr["by_channel"].items(), key=lambda kv: kv[1])[0] if tr["by_channel"] else ""
    plan_line = ""
    if plan["week"]:
        plan_line = (f"<p style='margin:4px 0'>Plan week {plan['week']}. Trials started since 18 Nov: <b>{plan['trials_since_start']}</b>"
                     + (f" against <b>{plan['target_to_date']}</b> planned for the weeks finished so far" if plan["target_to_date"] else "")
                     + f" (goal {plan['total_target']:,}).</p>")
    body = f"""<div style="font-family:Arial,sans-serif;color:#0F172A;max-width:620px">
<h2 style="margin:0 0 4px">Weekly scorecard</h2>
<p style="margin:0 0 16px;color:#475569">{period}, Lagos time</p>
{check}
<table style="border-collapse:collapse;font-size:15px;margin-bottom:8px">
<tr><td style="padding:4px 16px 4px 0">Free brand checks</td><td style="text-align:right"><b>{bc['total']}</b></td></tr>
<tr><td style="padding:4px 16px 4px 0">Trial requests</td><td style="text-align:right"><b>{card['trial_requests']}</b></td></tr>
<tr><td style="padding:4px 16px 4px 0">Trials started</td><td style="text-align:right"><b>{tr['total']}</b></td></tr>
<tr><td style="padding:4px 16px 4px 0">Trials started after a brand check</td><td style="text-align:right"><b>{tr['from_brand_check']}</b> ({_pct(tr['from_brand_check'], tr['total'])})</td></tr>
<tr><td style="padding:4px 16px 4px 0">Paid sign-ups</td><td style="text-align:right"><b>{paid['total']}</b></td></tr>
</table>
{plan_line}
{f"<p style='margin:4px 0'>Most trials came from <b>{html.escape(best)}</b>.</p>" if best else ""}
{_table("Trials started by channel", tr["by_channel"])}
{_table("Brand checks by channel", bc["by_channel"])}
{_table("Paid sign-ups by channel", paid["by_channel"]) if paid["total"] else ""}
<p style="margin:16px 0 4px"><b>Brand check health</b></p>
<p style="margin:0;font-size:14px">Finished {bc['done']}, failed {bc['failed']}, still waiting {bc['waiting']}, results emailed {bc['emailed']}, reused from an earlier check {bc['reused']}.</p>
<p style="margin:16px 0 0;font-size:13px;color:#475569">Left out as our own team's activity: {bc['team']} brand checks and {tr['team']} trials from brandslens.com addresses.
"Untagged" means the visitor arrived without campaign tags. Channels are shown as source / medium.</p>
<p style="margin:16px 0 0;font-size:13px;color:#475569">BrandsLens scorecard</p>
</div>"""
    return subject, body


def _from_addr() -> str:
    m = re.search(r"<([^>]+)>", TRIAL_FROM or "")
    addr = m.group(1) if m else (TRIAL_FROM or "").strip()
    return f"BrandsLens Scorecard <{addr}>" if addr else ""


def send_if_due(db: Session, now: datetime | None = None) -> bool:
    """Sends last week's scorecard once, on Monday from 09:00 Lagos (or the first run after it that week). Returns True if sent."""
    now = now or now_utc()
    local = aware(now).astimezone(LAGOS)
    if local.weekday() == 0 and local.hour < SEND_HOUR:
        return False
    to = recipients()
    if not to:
        return False
    _, _, monday = week_bounds(now)
    if get_setting(db, SENT_KEY, "") == monday.isoformat():
        return False
    card = build(db, now)
    subject, body = render(card)
    from . import mailer
    sent_any = False
    for addr in to:
        sent_any = mailer.send_email(addr, subject, body, from_addr=_from_addr(), reply_to=TRIAL_REPLY_TO) or sent_any
    if sent_any:
        set_setting(db, SENT_KEY, monday.isoformat(), updated_by="scorecard")
        db.commit()
        log.info("Weekly scorecard for week of %s sent to %s", monday, ", ".join(to))
    return sent_any
