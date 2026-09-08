"""app/services/billing.py — Stripe + Flutterwave, wired into the real MVP.

Deliberately fails soft: if no billing keys are configured, checkout raises a
clean, expected error rather than crashing, and organizations simply stay in
'trialing' status. This matches the "don't let missing APIs block launch"
principle applied everywhere else in this build — you can genuinely run this
in production for trial customers today and turn on real billing the moment
Stripe/Flutterwave accounts exist, without changing anything else.

Flutterwave replaced Paystack here as the local/African-card rail — this file
no longer talks to Paystack at all.
"""
from __future__ import annotations
import hmac
import json
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, FLUTTERWAVE_SECRET_KEY, FLUTTERWAVE_SECRET_HASH, FRONTEND_ORIGIN
from ..models import Organization, OrgMember, BillingEvent, now_utc

log = logging.getLogger("billing")

PLAN_CATALOG = {
    "standard": {"annual_usd": 1500, "monthly_usd": 143.75, "daily_usd": 4.93,
                "stripe_price_annual": "price_standard_annual", "stripe_price_monthly": "price_standard_monthly",
                "stripe_price_daily": "price_standard_daily",
                "flutterwave_plan_annual": "FLW_standard_annual", "flutterwave_plan_monthly": "FLW_standard_monthly",
                "flutterwave_plan_daily": "FLW_standard_daily"},
    "growth": {"annual_usd": 2500, "monthly_usd": 239.58, "daily_usd": 8.22,
              "stripe_price_annual": "price_growth_annual", "stripe_price_monthly": "price_growth_monthly",
              "stripe_price_daily": "price_growth_daily",
              "flutterwave_plan_annual": "FLW_growth_annual", "flutterwave_plan_monthly": "FLW_growth_monthly",
              "flutterwave_plan_daily": "FLW_growth_daily"},
    "professional": {"annual_usd": 3500, "monthly_usd": 335.42, "daily_usd": 11.51,
                     "stripe_price_annual": "price_professional_annual", "stripe_price_monthly": "price_professional_monthly",
                     "stripe_price_daily": "price_professional_daily",
                     "flutterwave_plan_annual": "FLW_professional_annual", "flutterwave_plan_monthly": "FLW_professional_monthly",
                     "flutterwave_plan_daily": "FLW_professional_daily"},
    # Enterprise deliberately has no entry here — there's no fixed price to
    # check out against. It's handled entirely by submit_enterprise_inquiry()
    # below, which emails a real conversation instead of charging a card.
}
CYCLE_KEYS = {"annual": ("annual_usd", "stripe_price_annual", "flutterwave_plan_annual"),
             "monthly": ("monthly_usd", "stripe_price_monthly", "flutterwave_plan_monthly"),
             "daily": ("daily_usd", "stripe_price_daily", "flutterwave_plan_daily")}


class BillingNotConfigured(Exception):
    """Raised when a checkout is attempted before Stripe/Flutterwave keys exist.
    The API turns this into a clear message, not a 500 — this is an expected
    state at MVP stage, not a bug."""


def submit_enterprise_inquiry(name: str, email: str, company: str, message: str) -> bool:
    """Enterprise has no fixed price — 'checkout' for this tier is a real
    conversation, not a card charge. This emails the inquiry directly rather
    than creating any billing record at all."""
    from .mailer import send_enterprise_inquiry
    return send_enterprise_inquiry(name, email, company, message)


def create_stripe_checkout(org: Organization, plan: str, cycle: str, customer_email: str) -> str:
    if not STRIPE_SECRET_KEY:
        raise BillingNotConfigured("Stripe isn't connected yet — please try again shortly or contact support.")
    if cycle not in CYCLE_KEYS:
        raise BillingNotConfigured("Billing cycle must be 'annual', 'monthly', or 'daily'.")
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    catalog = PLAN_CATALOG[plan]
    _, stripe_key, _ = CYCLE_KEYS[cycle]
    price_id = catalog[stripe_key]
    session = stripe.checkout.Session.create(
        mode="subscription", customer_email=customer_email, line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{FRONTEND_ORIGIN}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{FRONTEND_ORIGIN}/billing/cancelled",
        client_reference_id=org.id, metadata={"organization_id": org.id, "plan": plan, "cycle": cycle},
        subscription_data={"metadata": {"organization_id": org.id, "plan": plan}},
    )
    return session.url


def create_flutterwave_checkout(org: Organization, plan: str, cycle: str, customer_email: str) -> str:
    if not FLUTTERWAVE_SECRET_KEY:
        raise BillingNotConfigured("Flutterwave isn't connected yet — please try again shortly or contact support.")
    if cycle not in CYCLE_KEYS:
        raise BillingNotConfigured("Billing cycle must be 'annual', 'monthly', or 'daily'.")
    catalog = PLAN_CATALOG[plan]
    amount_key, _, flw_key = CYCLE_KEYS[cycle]
    payment_plan = catalog[flw_key]
    # tx_ref must be unique per attempt — Flutterwave uses it to dedupe retries.
    tx_ref = f"{org.id}-{plan}-{cycle}-{int(now_utc().timestamp())}"
    resp = httpx.post("https://api.flutterwave.com/v3/payments",
                      headers={"Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}"},
                      json={"tx_ref": tx_ref, "amount": catalog[amount_key], "currency": "USD",
                           "redirect_url": f"{FRONTEND_ORIGIN}/billing/success",
                           "payment_plan": payment_plan,
                           "customer": {"email": customer_email},
                           "meta": {"organization_id": org.id, "plan": plan, "cycle": cycle}}, timeout=20)
    resp.raise_for_status()
    return resp.json()["data"]["link"]


def verify_flutterwave_signature(signature_header: str) -> bool:
    # Flutterwave doesn't HMAC-sign the payload like Stripe does — you set a
    # "secret hash" in Dashboard > Settings > Webhooks, and Flutterwave
    # echoes it back verbatim in the verif-hash header on every call. Valid
    # only if it matches exactly.
    if not FLUTTERWAVE_SECRET_HASH:
        return False
    return hmac.compare_digest(FLUTTERWAVE_SECRET_HASH, signature_header or "")


def handle_stripe_webhook(db: Session, payload: bytes, sig_header: str) -> dict:
    import stripe
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception:  # noqa: BLE001
        log.warning("Rejected Stripe webhook: bad signature or payload")
        raise PermissionError("invalid_stripe_signature")

    etype, obj = event["type"], event["data"]["object"]
    org_id = obj.get("client_reference_id") or obj.get("metadata", {}).get("organization_id")
    plan = obj.get("metadata", {}).get("plan")

    if etype == "checkout.session.completed":
        _activate_plan(db, org_id, plan, "stripe", obj.get("customer"), obj.get("subscription"))
    elif etype == "customer.subscription.updated" and obj.get("status") in ("past_due", "unpaid"):
        _flag_payment_issue(db, org_id)
    elif etype == "customer.subscription.deleted":
        _downgrade(db, org_id)

    db.add(BillingEvent(organization_id=org_id or "", provider="stripe", event_type=etype, raw=json.dumps(event)[:8000]))
    db.commit()
    return {"received": True}


def handle_flutterwave_webhook(db: Session, payload: bytes, signature_header: str) -> dict:
    if not verify_flutterwave_signature(signature_header):
        raise PermissionError("invalid_flutterwave_signature")
    event = json.loads(payload)
    etype, data = event.get("event"), event.get("data", {})
    meta = data.get("meta") or data.get("meta_data") or {}
    org_id, plan = meta.get("organization_id"), meta.get("plan")

    if etype == "charge.completed" and data.get("status") == "successful":
        # The verif-hash proves the call came from Flutterwave, but not that
        # this specific transaction really cleared — Flutterwave recommends
        # re-querying the transaction server-side before granting access, so
        # a replayed or forged "successful" payload can't activate a plan.
        tx_id = data.get("id")
        verified = False
        if tx_id and FLUTTERWAVE_SECRET_KEY:
            resp = httpx.get(f"https://api.flutterwave.com/v3/transactions/{tx_id}/verify",
                             headers={"Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}"}, timeout=20)
            if resp.status_code == 200:
                vdata = resp.json().get("data", {})
                verified = vdata.get("status") == "successful" and vdata.get("amount", 0) >= data.get("amount", 0)
        if verified:
            _activate_plan(db, org_id, plan, "flutterwave", str(data.get("customer", {}).get("id", "")),
                           str(data.get("id", "")))
        else:
            log.warning("Flutterwave webhook claimed success but re-verification failed; not activating")
    elif etype in ("subscription.cancelled", "subscription.expired"):
        _downgrade(db, org_id)

    db.add(BillingEvent(organization_id=org_id or "", provider="flutterwave", event_type=etype or "unknown",
                        raw=json.dumps(event)[:8000]))
    db.commit()
    return {"received": True}


def _activate_plan(db: Session, org_id: str, plan: str, provider: str, customer_id: str, subscription_id: str) -> None:
    if not org_id:
        return
    org = db.get(Organization, org_id)
    if not org:
        return
    from .auth import PLAN_WORKSPACE_LIMIT, PLAN_KEYWORD_LIMIT
    org.plan = plan
    org.billing_provider = provider
    org.billing_customer_id = customer_id
    org.billing_subscription_id = subscription_id
    org.billing_status = "active"
    org.plan_activated_at = now_utc()
    org.workspace_limit = PLAN_WORKSPACE_LIMIT.get(plan, org.workspace_limit)
    org.keyword_limit = PLAN_KEYWORD_LIMIT.get(plan, org.keyword_limit)
    db.commit()


def _flag_payment_issue(db: Session, org_id: str) -> None:
    org = db.get(Organization, org_id) if org_id else None
    if org:
        org.billing_status = "past_due"
        db.commit()
        owner = db.scalar(select(OrgMember).where(OrgMember.organization_id == org.id, OrgMember.role == "owner"))
        if owner:
            from .mailer import send_payment_failed
            send_payment_failed(owner.email, owner.name, org.name)


def _downgrade(db: Session, org_id: str) -> None:
    org = db.get(Organization, org_id) if org_id else None
    if org:
        org.billing_status = "cancelled"
        org.plan_cancelled_at = now_utc()
        org.read_only_after = now_utc() + timedelta(days=30)
        db.commit()
