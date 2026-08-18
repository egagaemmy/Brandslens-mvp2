"""app/worker.py — the scheduler. Run as its own process: `python -m app.worker`

Every source runs on a sensible cadence for MVP volume. Nothing here requires
a message queue or Redis — APScheduler in a single process is genuinely
enough until you have real scale, and "genuinely enough" beats "impressive
but unnecessary" for an MVP.
"""
from __future__ import annotations
import asyncio
import logging
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from .db import SessionLocal, init_db
from .config import (CADENCE_NEWS, CADENCE_NAIRALAND, CADENCE_HACKERNEWS, CADENCE_REDDIT, CADENCE_YOUTUBE,
                     CADENCE_DOMAINS, CADENCE_TELEGRAM_FLUSH, CADENCE_X, CADENCE_SLA_SWEEP, TIMEZONE)
from .models import Workspace
from .services import pipeline, media_room
from .services.mailer import slack_alert
from .collectors import (news_collector, nairaland_collector, hackernews_collector, reddit_collector,
                         youtube_collector, domain_collector, telegram_collector, x_collector)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("worker")


def _each_workspace(db):
    return db.scalars(select(Workspace).where(Workspace.active == True)).all()  # noqa: E712


def run_collector(name: str, collect_fn) -> None:
    """Runs collect_fn once per workspace. The short pause between workspaces
    is deliberate: GDELT, Reddit, and YouTube are all external APIs with
    their own rate limits, and with more than a couple of workspaces,
    calling them back-to-back with zero spacing trips those limits — this
    happened for real in production (GDELT returning 429 Too Many Requests
    within seconds of the previous workspace's request)."""
    db = SessionLocal()
    try:
        workspaces = _each_workspace(db)
        for i, ws in enumerate(workspaces):
            try:
                candidates = collect_fn(db, ws)
                if candidates:
                    summary = pipeline.ingest_candidates(db, ws, candidates, source=name)
                    log.info("%s / %s: %s", name, ws.name, summary)
            except Exception:  # noqa: BLE001
                log.exception("%s failed for %s", name, ws.name)
            if i < len(workspaces) - 1:
                time.sleep(3)
    finally:
        db.close()


def flush_telegram() -> None:
    db = SessionLocal()
    try:
        for ws in _each_workspace(db):
            cands = telegram_collector.drain(ws.id)
            if cands:
                summary = pipeline.ingest_candidates(db, ws, cands, source="telegram")
                log.info("telegram / %s: %s", ws.name, summary)
    finally:
        db.close()


def sweep_sla() -> None:
    db = SessionLocal()
    try:
        breached = media_room.sweep_sla_breaches(db)
        for case in breached:
            slack_alert(f":alarm_clock: SLA BREACHED — Media Room case {case.id} "
                       f"({case.severity}) has been open past its {case.sla_target_hours}h target.")
    finally:
        db.close()


def start_background_loops() -> None:
    db = SessionLocal()
    channel_map = {ws.id: (ws.telegram_channels or []) for ws in db.scalars(select(Workspace)).all()}
    db.close()
    channel_map = {k: v for k, v in channel_map.items() if v}
    if channel_map:
        threading.Thread(target=lambda: asyncio.run(telegram_collector.start(channel_map)),
                         daemon=True, name="telegram").start()

    from .collectors import tipline_bot
    threading.Thread(target=lambda: asyncio.run(tipline_bot.start()), daemon=True, name="tipline").start()


def main() -> None:
    init_db()
    sched = BackgroundScheduler(timezone=TIMEZONE)
    sched.add_job(run_collector, "interval", minutes=CADENCE_NEWS, args=("news", news_collector.collect), id="news")
    sched.add_job(run_collector, "interval", minutes=CADENCE_NAIRALAND, args=("nairaland", nairaland_collector.collect), id="nairaland")
    sched.add_job(run_collector, "interval", minutes=CADENCE_HACKERNEWS, args=("hackernews", hackernews_collector.collect), id="hackernews")
    sched.add_job(run_collector, "interval", minutes=CADENCE_REDDIT, args=("reddit", reddit_collector.collect), id="reddit")
    sched.add_job(run_collector, "interval", minutes=CADENCE_YOUTUBE, args=("youtube", youtube_collector.collect), id="youtube")
    sched.add_job(run_collector, "interval", minutes=CADENCE_DOMAINS, args=("domains", domain_collector.collect), id="domains")
    sched.add_job(run_collector, "interval", minutes=CADENCE_X, args=("x", x_collector.collect), id="x")  # no-ops if X_ENABLED=false
    sched.add_job(flush_telegram, "interval", minutes=CADENCE_TELEGRAM_FLUSH, id="tg-flush")
    sched.add_job(sweep_sla, "interval", minutes=CADENCE_SLA_SWEEP, id="sla-sweep")
    sched.start()
    start_background_loops()
    log.info("MVP worker started — news %sm, nairaland %sm, hackernews %sm, reddit %sm, youtube %sm, domains %sm, SLA sweep %sm",
             CADENCE_NEWS, CADENCE_NAIRALAND, CADENCE_HACKERNEWS, CADENCE_REDDIT, CADENCE_YOUTUBE, CADENCE_DOMAINS, CADENCE_SLA_SWEEP)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        sched.shutdown()


if __name__ == "__main__":
    main()
