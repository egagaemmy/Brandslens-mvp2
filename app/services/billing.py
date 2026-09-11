"""app/services/billing.py — Stripe + Paystack + Flutterwave, wired into the real MVP.

Deliberately fails soft: if no billing keys are configured, checkout raises a
clean, expected error rather than crashing, and organizations simply stay in
'trialing' status. This matches the "don't let missing APIs block launch"
principle applied everywhere else in this build — you can genuinely run this
in production for trial customers today and turn on real billing the moment
Stripe/Paystack/Flutterwave accounts exist, without changing anything else.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, PAYSTACK_SECRET_KEY,
                      FLUTTERWAVE_SECRET_KEY, FLUTTERWAVE_WEBHOOK_HASH, FRONTEND_ORIGIN, APP_URL, NGN_PER_USD_RATE)
from ..models import Organization, OrgMember, BillingEvent, PendingSignup, now_utc

log = logging.getLogger("billing")

PLAN_CATALOG = {
    "standard": {"annual_usd": 1500, "monthly_usd": 143.75, "daily_usd": 4.93,
                "stripe_price_annual": "price_standard_annual", "stripe_price_monthly": "price_standard_monthly",
                "stripe_price_daily": "price_standard_daily",
                "paystack_plan_annual": "PLN_standard_annual", "paystack_plan_monthly": "PLN_standard_monthly",
                "paystack_plan_daily": "PLN_standard_daily",
                "flutterwave_plan_annual": "168960", "flutterwave_plan_monthly": "168959",
                "flutterwave_plan_daily": "168958"},
    "growth": {"annual_usd": 2500, "monthly_usd": 239.58, "daily_usd": 8.22,
              "stripe_price_annual": "price_growth_annual", "stripe_price_monthly": "price_growth_monthly",
              "stripe_price_daily": "price_growth_daily",
              "paystack_plan_annual": "PLN_growth_annual", "paystack_plan_monthly": "PLN_growth_monthly",
              "paystack_plan_daily": "PLN_growth_daily",
              "flutterwave_plan_annual": "168963", "flutterwave_plan_monthly": "168962",
              "flutterwave_plan_daily": "168961"},
    "professional": {"annual_usd": 3500, "monthly_usd": 335.42, "daily_usd": 11.51,
                     "stripe_price_annual": "price_professional_annual", "stripe_price_monthly": "price_professional_monthly",
                     "stripe_price_daily": "price_professional_daily",
                     "paystack_plan_annual": "PLN_professional_annual", "paystack_plan_monthly": "PLN_professional_monthly",
                     "paystack_plan_daily": "PLN_professional_daily",
                     "flutterwave_plan_annual": "168966", "flutterwave_plan_monthly": "168965",
                     "flutterwave_plan_daily": "168964"},
    # Enterprise deliberately has no entry here — there's no fixed price to
    # check out against. It's handled entirely by submit_enterprise_inquiry()
    # below, which emails a real conversation instead of charging a card.
}
CYCLE_KEYS = {"annual": ("annual_usd", "stripe_price_annual", "paystack_plan_annual", "flutterwave_plan_annual"),
             "monthly": ("monthly_usd", "stripe_price_monthly", "paystack_plan_monthly", "flutterwave_plan_monthly"),
             "daily": ("daily_usd", "stripe_price_daily", "paystack_plan_daily", "flutterwave_plan_daily")}
CYCLE_DAYS = {"annual": 365, "monthly": 30, "daily": 1}


class BillingNotConfigured(Exception):
    """Raised when a checkout is attempted before Stripe/Paystack keys exist.
    The API turns this into a clear message, not a 500 — this is an expected
    state at MVP stage, not a bug."""


def submit_enterprise_inquiry(name: str, designation: str, email: str, company: str,
                              message: str, preferred_meeting_time: str) -> bool:
    """Enterprise has no fixed price — 'checkout' for this tier is a real
    conversation, not a card charge. This emails the inquiry directly rather
    than creating any billing record at all."""
    from .mailer import send_enterprise_inquiry
    return send_enterprise_inquiry(name, designation, email, company, message, preferred_meeting_time)


def create_stripe_checkout(org: Organization, plan: str, cycle: str, customer_email: str) -> str:
    if not STRIPE_SECRET_KEY:
        raise BillingNotConfigured("Stripe isn't connected yet — please try again shortly or contact support.")
    if cycle not in CYCLE_KEYS:
        raise BillingNotConfigured("Billing cycle must be 'annual', 'monthly', or 'daily'.")
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    catalog = PLAN_CATALOG[plan]
    _, stripe_key, _, _ = CYCLE_KEYS[cycle]
    price_id = catalog[stripe_key]
    session = stripe.checkout.Session.create(
        mode="subscription", customer_email=customer_email, line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{APP_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_URL}/billing/cancelled",
        client_reference_id=org.id, metadata={"organization_id": org.id, "plan": plan, "cycle": cycle},
        subscription_data={"metadata": {"organization_id": org.id, "plan": plan}},
    )
    return session.url


def create_paystack_checkout(org: Organization, plan: str, cycle: str, customer_email: str) -> str:
    if not PAYSTACK_SECRET_KEY:
        raise BillingNotConfigured("Paystack isn't connected yet — please try again shortly or contact support.")
    if cycle not in CYCLE_KEYS:
        raise BillingNotConfigured("Billing cycle must be 'annual', 'monthly', or 'daily'.")
    catalog = PLAN_CATALOG[plan]
    _, _, paystack_key, _ = CYCLE_KEYS[cycle]
    plan_code = catalog[paystack_key]
    resp = httpx.post("https://api.paystack.co/transaction/initialize",
                      headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
                      json={"email": customer_email, "plan": plan_code,
                           "metadata": {"organization_id": org.id, "plan": plan, "cycle": cycle},
                           "callback_url": f"{APP_URL}/billing/success"}, timeout=20)
    resp.raise_for_status()
    return resp.json()["data"]["authorization_url"]


def _flutterwave_initiate_payment(tx_ref: str, amount: float, redirect_url: str, customer_email: str,
                                  meta: dict, payment_plan: str | None, currency: str = "USD",
                                  payment_options: str | None = None) -> str:
    """The actual call to Flutterwave's payment initiation endpoint — shared
    by both an existing organization's checkout and a brand-new prospect's
    pay-first signup checkout. The only real difference between those two
    callers is what goes in tx_ref/meta, not how the call itself or its
    error handling works, so this is the one place that logic lives.

    currency/payment_options default to the original USD, unrestricted
    behavior — only the NGN fallback path (bank transfer, USSD, direct
    account payment) passes anything different, since those payment
    methods are only available for NGN-denominated charges at all."""
    if not FLUTTERWAVE_SECRET_KEY:
        raise BillingNotConfigured("Flutterwave isn't connected yet — please try again shortly or contact support.")
    body = {"tx_ref": tx_ref, "amount": amount, "currency": currency, "redirect_url": redirect_url,
           "customer": {"email": customer_email}, "meta": meta}
    if payment_plan:
        body["payment_plan"] = payment_plan
    if payment_options:
        body["payment_options"] = payment_options
    try:
        resp = httpx.post("https://api.flutterwave.com/v3/payments",
                          headers={"Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}"}, json=body, timeout=20)
        resp.raise_for_status()
        return resp.json()["data"]["link"]
    except httpx.HTTPStatusError as e:
        # Surface Flutterwave's own error message rather than a blind 500 —
        # this is genuinely the only way to tell "invalid plan ID" apart
        # from "account not approved for USD" apart from a dozen other real
        # reasons this specific call can fail, and none of them look the
        # same to the customer without this.
        try:
            flw_message = e.response.json().get("message", str(e))
        except Exception:  # noqa: BLE001
            flw_message = str(e)
        log.error("Flutterwave checkout rejected: %s (status %s)", flw_message, e.response.status_code)
        raise BillingNotConfigured(f"Flutterwave couldn't start this payment: {flw_message}")
    except httpx.RequestError as e:
        log.exception("Flutterwave checkout request failed to even reach Flutterwave")
        raise BillingNotConfigured("Couldn't reach Flutterwave right now — please try again shortly.") from e


MAX_QUANTITY = {"daily": 90, "monthly": 24, "annual": 5}


def create_flutterwave_checkout_for_signup(pending_id: str, plan: str, cycle: str, quantity: int, customer_email: str,
                                           pay_in_ngn: bool = False) -> tuple[str, str]:
    """The pay-first equivalent of create_flutterwave_checkout — for a
    prospect who doesn't have an account yet. No Organization exists at
    this point; pending_id (a PendingSignup row's id) is what the webhook
    uses to find the held signup details and actually create the real
    account, once payment is genuinely confirmed. Kept as a separate
    function rather than added as an optional parameter to the existing
    one, since the two have a meaningfully different failure mode: if this
    one's webhook never arrives, there's no account to leave half-activated
    — there's simply nothing created at all, which is the safe direction
    for this to fail in.

    pay_in_ngn — same fallback and same reasoning as create_flutterwave_checkout:
    bank transfer/USSD/account payment only exist for NGN charges, so this
    converts the price and switches payment methods, always as a one-time
    charge regardless of quantity.

    Returns (checkout_url, tx_ref) — the caller needs tx_ref too, to store
    on the PendingSignup row so the post-payment status check can find it."""
    if cycle not in CYCLE_KEYS:
        raise BillingNotConfigured("Billing cycle must be 'annual', 'monthly', or 'daily'.")
    if quantity < 1 or quantity > MAX_QUANTITY.get(cycle, 1):
        raise BillingNotConfigured(f"Quantity must be between 1 and {MAX_QUANTITY.get(cycle, 1)} for {cycle} billing.")
    catalog = PLAN_CATALOG[plan]
    price_key, _, _, flutterwave_key = CYCLE_KEYS[cycle]
    base_price = catalog[price_key]
    tx_ref = f"blens-signup-{pending_id}-{int(now_utc().timestamp())}"
    if pay_in_ngn:
        url = _flutterwave_initiate_payment(
            tx_ref=tx_ref, amount=round(base_price * quantity * NGN_PER_USD_RATE, 2),
            redirect_url=f"{APP_URL}/signup/complete?tx_ref={tx_ref}", customer_email=customer_email,
            meta={"pending_signup_id": pending_id, "plan": plan, "cycle": cycle, "quantity": quantity, "one_time": True},
            payment_plan=None, currency="NGN", payment_options="card, banktransfer, ussd, account",
        )
        return url, tx_ref
    url = _flutterwave_initiate_payment(
        tx_ref=tx_ref, amount=round(base_price * quantity, 2),
        redirect_url=f"{APP_URL}/signup/complete?tx_ref={tx_ref}", customer_email=customer_email,
        meta={"pending_signup_id": pending_id, "plan": plan, "cycle": cycle, "quantity": quantity},
        payment_plan=catalog[flutterwave_key] if quantity == 1 else None,
    )
    return url, tx_ref


def create_flutterwave_checkout(org: Organization, plan: str, cycle: str, customer_email: str,
                                quantity: int = 1, pay_in_ngn: bool = False) -> str:
    """Flutterwave's Standard Checkout: a single POST returns a hosted
    payment link, same shape as Paystack's initialize call.

    quantity == 1 (the default) is an ordinary recurring subscription: the
    plan reference is passed as `payment_plan` so Flutterwave handles the
    recurring billing itself, same as before.

    quantity > 1 is genuinely different — a customer paying for, say, 3
    months up front isn't subscribing to a recurring plan at that rate;
    they're making a one-time payment covering 3 real months of access.
    So `payment_plan` is deliberately omitted (this is NOT recurring),
    the amount is the base price multiplied by quantity, and `quantity` +
    `cycle` travel in `meta` so the webhook can calculate exactly when
    this purchase's access should lapse if it isn't renewed.

    pay_in_ngn is the fallback for a card that can't complete an
    international USD charge at all — bank transfer, USSD, and direct
    account payment only exist as Flutterwave payment methods for
    NGN-denominated charges, so this converts the USD price at
    NGN_PER_USD_RATE and switches to those methods. Card is included here
    too, deliberately: the original decline is specifically about the
    charge being *international* — the same card, charged in Naira
    instead of dollars, is no longer an international transaction from
    the bank's point of view, and may genuinely succeed here even though
    it failed on the USD path. Always a one-time charge, even at
    quantity 1: the existing payment_plan IDs are USD-denominated and
    wouldn't apply to an NGN charge, so there's no recurring option on
    this path — it's genuinely a fallback, not a parallel subscription
    mechanism."""
    if cycle not in CYCLE_KEYS:
        raise BillingNotConfigured("Billing cycle must be 'annual', 'monthly', or 'daily'.")
    if quantity < 1:
        raise BillingNotConfigured("Quantity must be at least 1.")
    catalog = PLAN_CATALOG[plan]
    price_key, _, _, flutterwave_key = CYCLE_KEYS[cycle]
    base_price = catalog[price_key]
    tx_ref = f"blens-{org.id}-{plan}-{cycle}-{int(now_utc().timestamp())}"
    if pay_in_ngn:
        return _flutterwave_initiate_payment(
            tx_ref=tx_ref, amount=round(base_price * quantity * NGN_PER_USD_RATE, 2),
            redirect_url=f"{APP_URL}/billing/success", customer_email=customer_email,
            meta={"organization_id": org.id, "plan": plan, "cycle": cycle, "quantity": quantity, "one_time": True},
            payment_plan=None, currency="NGN", payment_options="card, banktransfer, ussd, account",
        )
    return _flutterwave_initiate_payment(
        tx_ref=tx_ref, amount=round(base_price * quantity, 2),
        redirect_url=f"{APP_URL}/billing/success", customer_email=customer_email,
        meta={"organization_id": org.id, "plan": plan, "cycle": cycle, "quantity": quantity},
        payment_plan=catalog[flutterwave_key] if quantity == 1 else None,
    )


def verify_paystack_signature(payload: bytes, signature_header: str) -> bool:
    if not PAYSTACK_SECRET_KEY:
        return False
    computed = hmac.new(PAYSTACK_SECRET_KEY.encode(), payload, hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature_header or "")


def verify_flutterwave_signature(signature_header: str) -> bool:
    """Flutterwave's webhook verification isn't a computed cryptographic
    signature like Stripe/Paystack — it's a pre-shared secret hash you set
    once in the Flutterwave dashboard, sent back verbatim in the verif-hash
    header. Verification is just confirming it matches, done as a
    constant-time comparison so this check itself can't leak the secret
    through response-time differences."""
    if not FLUTTERWAVE_WEBHOOK_HASH:
        return False
    return hmac.compare_digest(FLUTTERWAVE_WEBHOOK_HASH, signature_header or "")


def _verify_flutterwave_transaction(transaction_id: str, expected_amount: float, expected_currency: str) -> bool:
    """Flutterwave's own webhook documentation is explicit that a webhook
    alone should never be trusted for a payment decision — re-verify the
    transaction server-side against their API before treating it as real.
    This matters more for Flutterwave than Paystack/Stripe specifically
    because Flutterwave's webhook check above is a shared secret, not a
    cryptographic signature of the actual payload — this second call
    confirms the amount and status genuinely match what we expect, not
    just that the request carried the right header."""
    try:
        resp = httpx.get(f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify",
                         headers={"Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}"}, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return (data.get("status") == "successful"
               and data.get("currency") == expected_currency
               and float(data.get("amount", 0)) >= float(expected_amount))
    except Exception:  # noqa: BLE001 — a verification failure must never activate a plan
        log.exception("Flutterwave server-side transaction verification failed for %s", transaction_id)
        return False


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


def handle_paystack_webhook(db: Session, payload: bytes, signature_header: str) -> dict:
    if not verify_paystack_signature(payload, signature_header):
        raise PermissionError("invalid_paystack_signature")
    event = json.loads(payload)
    etype, data = event.get("event"), event.get("data", {})
    meta = data.get("metadata") or {}
    org_id, plan = meta.get("organization_id"), meta.get("plan")

    if etype == "charge.success":
        _activate_plan(db, org_id, plan, "paystack", data.get("customer", {}).get("customer_code"),
                       data.get("plan_object", {}).get("plan_code"))
    elif etype == "subscription.disable":
        _downgrade(db, org_id)

    db.add(BillingEvent(organization_id=org_id or "", provider="paystack", event_type=etype, raw=json.dumps(event)[:8000]))
    db.commit()
    return {"received": True}


def handle_flutterwave_webhook(db: Session, payload: bytes, signature_header: str) -> dict:
    if not verify_flutterwave_signature(signature_header):
        raise PermissionError("invalid_flutterwave_signature")
    event = json.loads(payload)
    data = event.get("data", {})
    etype = event.get("event", "")
    meta = data.get("meta") or {}
    org_id, plan = meta.get("organization_id"), meta.get("plan")
    pending_signup_id = meta.get("pending_signup_id")
    cycle, quantity = meta.get("cycle", "annual"), int(meta.get("quantity", 1))

    if etype == "charge.completed" and data.get("status") == "successful":
        # Defense in depth, per Flutterwave's own guidance: the webhook
        # signature only proves the request came from Flutterwave, not that
        # this specific transaction is genuinely real — re-verify server-side
        # before ever activating anything, or creating any account at all.
        verified = _verify_flutterwave_transaction(str(data.get("id", "")), data.get("amount", 0), data.get("currency", "USD"))
        if verified:
            is_one_time = quantity > 1 or bool(meta.get("one_time"))
            paid_until = (now_utc() + timedelta(days=CYCLE_DAYS.get(cycle, 365) * quantity)) if is_one_time else None
            if pending_signup_id:
                # Pay-first signup: the account doesn't exist yet — this is
                # the one place it actually gets created, only now that
                # payment is genuinely confirmed. Safe to call more than
                # once (e.g. a retried webhook): completed_token being
                # already set is treated as "already handled."
                from . import auth
                pending = db.get(PendingSignup, pending_signup_id)
                if pending and not pending.completed_token:
                    try:
                        member, token = auth.create_account_from_pending_signup(db, pending, paid_until=paid_until)
                        pending.completed_token = token
                        pending.completed_member_id = member.id
                        org_id = member.organization_id
                        db.commit()
                    except Exception:  # noqa: BLE001 — a failure here must never silently swallow a real payment
                        log.exception("Failed to create account from pending signup %s after confirmed payment", pending_signup_id)
                elif pending:
                    org_id = db.get(OrgMember, pending.completed_member_id).organization_id if pending.completed_member_id else org_id
            else:
                _activate_plan(db, org_id, plan, "flutterwave", str(data.get("customer", {}).get("id", "")),
                               str(data.get("id", "")), paid_until=paid_until)
        else:
            log.warning("Flutterwave webhook claimed success but server-side verification disagreed — nothing activated or created for %s",
                       pending_signup_id or org_id)

    db.add(BillingEvent(organization_id=org_id or "", provider="flutterwave", event_type=etype, raw=json.dumps(event)[:8000]))
    db.commit()
    return {"received": True}


def _activate_plan(db: Session, org_id: str, plan: str, provider: str, customer_id: str, subscription_id: str,
                   paid_until=None) -> None:
    if not org_id:
        return
    org = db.get(Organization, org_id)
    if not org:
        return
    is_first_activation = org.billing_status != "active"
    from .auth import PLAN_WORKSPACE_LIMIT, PLAN_KEYWORD_LIMIT
    org.plan = plan
    org.billing_provider = provider
    org.billing_customer_id = customer_id
    org.billing_subscription_id = subscription_id
    org.billing_status = "active"
    org.plan_activated_at = now_utc()
    org.paid_until = paid_until
    org.workspace_limit = PLAN_WORKSPACE_LIMIT.get(plan, org.workspace_limit)
    org.keyword_limit = PLAN_KEYWORD_LIMIT.get(plan, org.keyword_limit)
    db.commit()
    if is_first_activation:
        # Only a genuine first activation — never a renewal or a plan
        # change for a customer who's already active, which would
        # otherwise send "Welcome to BrandsLens" to someone who's been a
        # paying customer for months.
        owner = db.scalar(select(OrgMember).where(OrgMember.organization_id == org_id, OrgMember.role == "owner"))
        if owner:
            from .mailer import send_welcome_email
            send_welcome_email(owner.email, owner.name, plan)


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
