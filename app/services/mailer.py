"""Outbound alerts and emails. Slack for instant team alerts; Resend for
anything that needs to land in someone's inbox (trial reminders, payment
issues) — both fail silently (log + return False) if unconfigured, so a
missing API key never breaks the rest of the app."""
import logging
import httpx
from ..config import SLACK_WEBHOOK_DEFAULT, RESEND_API_KEY, MAIL_FROM
from ..branding import BRAND

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


def send_email(to: str, subject: str, html_body: str) -> bool:
    if not RESEND_API_KEY:
        log.info("No RESEND_API_KEY configured — email suppressed: %r to %s", subject, to)
        return False
    try:
        httpx.post("https://api.resend.com/emails",
                  headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                  json={"from": MAIL_FROM, "to": [to], "subject": subject, "html": html_body},
                  timeout=15).raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        log.exception("Email send failed to %s", to)
        return False


def _wrap(inner: str) -> str:
    """Minimal branded HTML wrapper, reusing the same colors as everywhere else."""
    return f"""<div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:32px 24px">
      <div style="color:#{BRAND['amber_dark']};font-weight:700;font-size:13px;letter-spacing:2px">{BRAND['name'].upper()}</div>
      <div style="margin-top:18px;color:#1F2937;font-size:14px;line-height:1.6">{inner}</div>
      <div style="margin-top:28px;padding-top:16px;border-top:1px solid #E5E7EB;color:#94A3B8;font-size:12px">{BRAND['tagline']}</div>
    </div>"""


def send_password_reset(to: str, name: str, reset_link: str) -> bool:
    body = _wrap(f"""<p>Hi {name},</p>
      <p>Someone requested a password reset for your BrandsLens account. If this was you,
      click below to choose a new password — this link works once and expires in 2 hours.</p>
      <p><a href="{reset_link}" style="background:#{BRAND['amber']};color:#0B0F17;padding:10px 20px;
      border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">Reset your password</a></p>
      <p style="color:#94A3B8;font-size:12px">If you didn't request this, you can safely ignore this email —
      your password hasn't been changed.</p>""")
    return send_email(to, "Reset your BrandsLens password", body)


def send_enterprise_inquiry(name: str, email: str, company: str, message: str) -> bool:
    """Enterprise has no self-serve checkout — this is the entire 'purchase
    flow' for that tier: a real message, to a real inbox, to start a real
    conversation about custom pricing."""
    from ..config import ENTERPRISE_INQUIRY_EMAIL
    body = _wrap(f"""<p><b>New Enterprise inquiry</b></p>
      <p><b>Name:</b> {name}<br><b>Email:</b> {email}<br><b>Company:</b> {company}</p>
      <p><b>Message:</b><br>{message}</p>""")
    return send_email(ENTERPRISE_INQUIRY_EMAIL, f"Enterprise inquiry from {company}", body)


def send_payment_failed(to: str, name: str, org_name: str) -> bool:
    body = _wrap(f"""<p>Hi {name},</p>
      <p>We couldn't process your latest payment for <b>{org_name}</b>. Please update your
      billing details to avoid any interruption to your monitoring.</p>""")
    return send_email(to, "Action needed: BrandsLens payment failed", body)
