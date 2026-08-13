"""Outbound alerts — Slack webhook only for MVP (free, instant to set up).
Add email (Resend) once you're past pure notification and into digests/reports."""
import logging
import httpx
from ..config import SLACK_WEBHOOK_DEFAULT

log = logging.getLogger("mailer")

def slack_alert(text: str, webhook: str = "") -> bool:
    hook = webhook or SLACK_WEBHOOK_DEFAULT
    if not hook:
        log.info("No Slack webhook configured — alert suppressed: %s", text[:80])
        return False
    try:
        httpx.post(hook, json={"text": text}, timeout=10).raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        log.exception("Slack alert failed")
        return False
