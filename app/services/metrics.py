"""metrics.py — Advertising Value Equivalency (AVE) and Share of Voice
(SOV) calculations. Both are standard PR/comms industry metrics; this
module implements them using real data already captured elsewhere
(each incident's estimated reach, the existing Competitors feature)
rather than inventing numbers with no basis.

AVE: what the equivalent paid advertising space would have cost, for
the reach a mention actually got — reach × CPM rate, then multiplied
by an editorial credibility multiplier, since earned coverage is
conventionally valued higher than the same reach bought as an ad.

SOV: what share of all tracked conversation (a brand's own mentions
plus its tracked competitors' mentions) belongs to that brand.
"""
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Incident, Competitor, CompetitorMention, Workspace

# Used only when an incident's own reach wasn't captured (reach == 0) —
# a conservative, platform-typical audience-size estimate so a real
# mention doesn't just get silently valued at zero. Deliberately a
# module-level default rather than per-workspace configuration for now;
# the CPM rate and multiplier below are where the real customization
# lives.
FALLBACK_REACH_BY_PLATFORM = {
    "News": 8000, "Twitter": 2000, "X (Twitter)": 2000, "Facebook": 1500,
    "Instagram": 1500, "Reddit": 1000, "Nairaland": 1200, "Hacker News": 1500,
    "YouTube": 3000, "Telegram": 500,
}
DEFAULT_FALLBACK_REACH = 800


def _effective_reach(incident: Incident) -> int:
    if incident.reach and incident.reach > 0:
        return incident.reach
    return FALLBACK_REACH_BY_PLATFORM.get(incident.platform, DEFAULT_FALLBACK_REACH)


def calculate_ave(incidents: list[Incident], workspace: Workspace) -> dict:
    """Total AVE for a set of incidents, plus a severity breakdown so a
    report or dashboard can show not just one number but where the
    value is actually coming from."""
    by_severity: dict[str, float] = Counter()
    total = 0.0
    for inc in incidents:
        reach = _effective_reach(inc)
        value = (reach / 1000) * workspace.ave_cpm_rate * workspace.ave_multiplier
        total += value
        by_severity[inc.severity] += value
    return {
        "total": round(total, 2),
        "currency": "USD",
        "mention_count": len(incidents),
        "cpm_rate": workspace.ave_cpm_rate,
        "multiplier": workspace.ave_multiplier,
        "breakdown_by_severity": {k: round(v, 2) for k, v in by_severity.items()},
    }


def calculate_sov(db: Session, workspace_id: str, start: datetime | None, end: datetime | None) -> dict:
    """Share of Voice across the brand's own mentions and every
    competitor tracked in this workspace. start/end of None means no
    bound in that direction — 'all time' — matching how date filtering
    already works elsewhere in this codebase, rather than requiring
    every caller to invent a wide default range."""
    from sqlalchemy import func

    def _count(model, extra_where):
        q = select(func.count()).select_from(model).where(*extra_where)
        if start:
            q = q.where(model.posted_at >= start)
        if end:
            q = q.where(model.posted_at <= end)
        return db.scalar(q) or 0

    brand_count = _count(Incident, [Incident.workspace_id == workspace_id])

    competitors = db.scalars(select(Competitor).where(Competitor.workspace_id == workspace_id)).all()
    competitor_counts: dict[str, int] = {}
    for comp in competitors:
        competitor_counts[comp.name] = _count(CompetitorMention, [CompetitorMention.competitor_id == comp.id])

    total = brand_count + sum(competitor_counts.values())
    sov_percent = round((brand_count / total) * 100, 1) if total > 0 else 100.0
    return {
        "sov_percent": sov_percent,
        "brand_mentions": brand_count,
        "competitor_mentions": competitor_counts,
        "total_tracked_mentions": total,
    }
