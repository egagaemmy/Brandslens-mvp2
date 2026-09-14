"""Outbound alerts and emails. Slack for instant team alerts; Resend for
anything that needs to land in someone's inbox (trial reminders, payment
issues) — both fail silently (log + return False) if unconfigured, so a
missing API key never breaks the rest of the app."""
import logging
import hashlib
import json
import httpx
from ..config import (SLACK_WEBHOOK_DEFAULT, RESEND_API_KEY, MAIL_FROM, MAILCHIMP_API_KEY, MAILCHIMP_SERVER_PREFIX,
                     MAILCHIMP_LIST_ID, APP_URL, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_CLAIM_EMAIL)
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


def send_push_notification(endpoint: str, p256dh_key: str, auth_key: str, title: str, body: str, url: str) -> bool:
    """Sends one Web Push message to one browser subscription. Returns
    False on ANY failure, including the specific, very normal case of a
    subscription that's simply expired (browsers rotate these over time —
    the caller is expected to delete that subscription row when this
    happens, not treat it as an alarming error)."""
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        log.info("Push notifications not configured — alert suppressed: %s", title)
        return False
    try:
        from pywebpush import webpush, WebPushException
        webpush(
            subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh_key, "auth": auth_key}},
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{VAPID_CLAIM_EMAIL}"},
        )
        return True
    except Exception:  # noqa: BLE001 — includes WebPushException for an expired/invalid subscription
        log.info("Push notification failed (subscription may have expired) for endpoint ending in ...%s", endpoint[-12:])
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


DEFAULT_WELCOME_SUBJECT = "Welcome to BrandsLens — here's what we're watching for you"
DEFAULT_WELCOME_BODY = """<p>Hi {{first_name}},</p>
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
      <p>Your <strong>{{plan}}</strong> workspace is set up and ready — log in any time to see
      what we're already tracking for you.</p>
      <p><a href="{{app_url}}/login" style="background:#{{amber}};color:#0B0F17;padding:10px 20px;
      border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">Go to your dashboard</a></p>
      <p>You're not just protecting a brand. You're protecting everything built to earn the trust behind it.</p>
      <p>Welcome aboard.<br>The BrandsLens Team</p>"""


def send_welcome_email(db, to: str, name: str, plan: str) -> bool:
    """Sent the moment a real account is actually created — whether through
    ordinary signup or the pay-first flow, once payment is confirmed. This
    is the one email that should always fire for a brand-new customer.

    subject/body come from the admin-editable template if one has been
    saved through the Super Admin dashboard, otherwise the defaults above
    — same safe-override pattern used everywhere else in this feature.
    {{placeholders}} are filled in here, at send time, regardless of
    which version (admin-edited or default) is actually being sent."""
    from .settings import get_setting
    first_name = name.split(" ")[0] if name else "there"
    subject_template = get_setting(db, "email:welcome_subject", DEFAULT_WELCOME_SUBJECT)
    body_template = get_setting(db, "email:welcome_body", DEFAULT_WELCOME_BODY)
    placeholders = {"{{first_name}}": first_name, "{{plan}}": plan.capitalize(),
                    "{{app_url}}": APP_URL, "{{amber}}": BRAND["amber"]}
    subject = subject_template
    body_html = body_template
    for token, real_value in placeholders.items():
        subject = subject.replace(token, real_value)
        body_html = body_html.replace(token, real_value)
    return send_email(to, subject, _wrap(body_html))


DEFAULT_MENTION_ALERT_SUBJECT = "{{severity}} mention detected — {{workspace_name}}"
DEFAULT_MENTION_ALERT_BODY = """<p>Hi {{first_name}},</p>
      <p>A new <strong>{{severity}}</strong> severity mention was just detected for <strong>{{workspace_name}}</strong>.</p>
      <p><strong>{{platform}}</strong><br>{{title}}</p>
      <p><a href="{{incident_url}}" style="background:#{{amber}};color:#0B0F17;padding:10px 20px;
      border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">View in BrandsLens</a></p>
      <p style="color:#94A3B8;font-size:12px">You're receiving this because email alerts are turned on for your
      account. You can turn these off anytime in Settings.</p>"""


def send_mention_alert_email(db, to: str, name: str, severity: str, workspace_name: str,
                             platform: str, title: str, incident_url: str) -> bool:
    """A new-mention alert — the reliable channel meant to reach everyone
    regardless of device, alongside push for those who've opted into that
    too. Only fires for HIGH/MEDIUM (see pipeline.py) — sending one of
    these for every single WATCH-tier mention would flood an active
    brand's inbox with routine, low-signal noise rather than genuinely
    useful alerts."""
    from .settings import get_setting
    first_name = name.split(" ")[0] if name else "there"
    subject_template = get_setting(db, "email:mention_alert_subject", DEFAULT_MENTION_ALERT_SUBJECT)
    body_template = get_setting(db, "email:mention_alert_body", DEFAULT_MENTION_ALERT_BODY)
    placeholders = {"{{first_name}}": first_name, "{{severity}}": severity, "{{workspace_name}}": workspace_name,
                    "{{platform}}": platform, "{{title}}": title, "{{incident_url}}": incident_url,
                    "{{amber}}": BRAND["amber"]}
    subject, body_html = subject_template, body_template
    for token, real_value in placeholders.items():
        subject = subject.replace(token, real_value)
        body_html = body_html.replace(token, real_value)
    return send_email(to, subject, _wrap(body_html))


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


def send_enterprise_inquiry(db, name: str, designation: str, email: str, company: str,
                            message: str, preferred_meeting_time: str) -> bool:
    """Enterprise has no self-serve checkout — this is the entire 'purchase
    flow' for that tier: a real message, to a real inbox, to start a real
    conversation about custom pricing."""
    from ..config import ENTERPRISE_INQUIRY_EMAIL
    from .settings import get_setting
    destination = get_setting(db, "notifications:enterprise_inquiry_email", ENTERPRISE_INQUIRY_EMAIL)
    designation_line = f"<br><b>Designation:</b> {designation}" if designation else ""
    meeting_line = (f"<p><b>Preferred meeting time:</b> {preferred_meeting_time}</p>"
                    if preferred_meeting_time else "")
    body = _wrap(f"""<p><b>New Enterprise inquiry</b></p>
      <p><b>Name:</b> {name}{designation_line}<br><b>Email:</b> {email}<br><b>Company:</b> {company}</p>
      {meeting_line}
      <p><b>Message:</b><br>{message}</p>""")
    return send_email(destination, f"Enterprise inquiry from {company}", body)


def send_payment_failed(to: str, name: str, org_name: str) -> bool:
    body = _wrap(f"""<p>Hi {name},</p>
      <p>We couldn't process your latest payment for <b>{org_name}</b>. Please update your
      billing details to avoid any interruption to your monitoring.</p>""")
    return send_email(to, "Action needed: BrandsLens payment failed", body)
