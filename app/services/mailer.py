"""Outbound alerts and emails. Slack for instant team alerts; Resend for
anything that needs to land in someone's inbox (trial reminders, payment
issues) — both fail silently (log + return False) if unconfigured, so a
missing API key never breaks the rest of the app."""
import logging
import hashlib
import httpx
from ..config import SLACK_WEBHOOK_DEFAULT, RESEND_API_KEY, MAIL_FROM, MAILCHIMP_API_KEY, MAILCHIMP_SERVER_PREFIX, MAILCHIMP_LIST_ID, APP_URL
from ..branding import BRAND

log = logging.getLogger("mailer")

def add_to_mailchimp(email: str, name: str = "") -> bool:
    """Mailchimp needs real authentication and its own request shape — a
    generic webhook was never going to satisfy that. Uses the documented
    upsert pattern (PUT to a hash of the lowercased email) so a repeat
    signup updates rather than errors, and status_if_new (not status)
    specifically to avoid ever silently re-subscribing someone who
    unsubscribed on Mailchimp's own side previously. name (if given) is
    passed as the FNAME merge field, so campaigns — like the welcome
    email — can greet the subscriber by name."""
    if not (MAILCHIMP_API_KEY and MAILCHIMP_SERVER_PREFIX and MAILCHIMP_LIST_ID):
        log.info("Mailchimp not configured — newsletter forward suppressed for %s", email)
        return False
    subscriber_hash = hashlib.md5(email.lower().encode()).hexdigest()
    url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members/{subscriber_hash}"
    payload = {"email_address": email, "status_if_new": "subscribed"}
    if name:
        payload["merge_fields"] = {"FNAME": name}
    try:
        httpx.put(url, auth=("anystring", MAILCHIMP_API_KEY), json=payload, timeout=10).raise_for_status()
        return True
    except Exception:  # noqa: BLE001 — the subscriber is already safely stored in our own database regardless
        log.exception("Mailchimp forward failed for %s", email)
        return False


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


def send_welcome_email(to: str, name: str, plan: str) -> bool:
    """Sent the moment a real account is actually created — whether through
    ordinary signup or the pay-first flow, once payment is confirmed. This
    is the one email that should always fire for a brand-new customer."""
    first_name = name.split(" ")[0] if name else "there"
    body = _wrap(f"""<p>Hi {first_name},</p>
      <p>Welcome to BrandsLens. You've just joined a community that takes something seriously most
      people overlook entirely: what's actually being said about a brand when nobody's watching.</p>
      <p>Ninety six percent of brand crises spread internationally within twenty four hours. Brand
      impersonation has surged three hundred sixty percent since 2020. And here in Nigeria, businesses
      face over four thousand cyberattacks every single week. Most of this happens quietly — in comments,
      on lookalike domains, in conversations a brand never sees until it's too late.</p>
      <p>BrandsLens exists to close that gap. We watch continuously, across news, social platforms, and
      forums. We score what we find with real judgment, not just keywords. And when something genuinely
      matters, we help you act within hours, not days — brands that respond within two hours see sixty one
      percent better recovery than those who wait.</p>
      <p>Your <strong>{plan.capitalize()}</strong> workspace is set up and ready — log in any time to see
      what we're already tracking for you.</p>
      <p><a href="{APP_URL}/login" style="background:#{BRAND['amber']};color:#0B0F17;padding:10px 20px;
      border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">Go to your dashboard</a></p>
      <p>You're not just protecting a brand. You're protecting everything built to earn the trust behind it.</p>
      <p>Welcome aboard.<br>The BrandsLens Team</p>""")
    return send_email(to, "Welcome to BrandsLens — here's what we're watching for you", body)


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


def send_enterprise_inquiry(name: str, designation: str, email: str, company: str,
                            message: str, preferred_meeting_time: str) -> bool:
    """Enterprise has no self-serve checkout — this is the entire 'purchase
    flow' for that tier: a real message, to a real inbox, to start a real
    conversation about custom pricing."""
    from ..config import ENTERPRISE_INQUIRY_EMAIL
    designation_line = f"<br><b>Designation:</b> {designation}" if designation else ""
    meeting_line = (f"<p><b>Preferred meeting time:</b> {preferred_meeting_time}</p>"
                    if preferred_meeting_time else "")
    body = _wrap(f"""<p><b>New Enterprise inquiry</b></p>
      <p><b>Name:</b> {name}{designation_line}<br><b>Email:</b> {email}<br><b>Company:</b> {company}</p>
      {meeting_line}
      <p><b>Message:</b><br>{message}</p>""")
    return send_email(ENTERPRISE_INQUIRY_EMAIL, f"Enterprise inquiry from {company}", body)


def send_payment_failed(to: str, name: str, org_name: str) -> bool:
    body = _wrap(f"""<p>Hi {name},</p>
      <p>We couldn't process your latest payment for <b>{org_name}</b>. Please update your
      billing details to avoid any interruption to your monitoring.</p>""")
    return send_email(to, "Action needed: BrandsLens payment failed", body)
