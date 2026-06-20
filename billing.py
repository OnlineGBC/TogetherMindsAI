"""
billing.py
----------
Phase 4 Step 4 — clinician subscription billing via Stripe.

Three tiers (see config): free / plus ($10, AI analysis) / pro ($25, + recording).
Payment is handled entirely by Stripe Checkout + the Stripe-hosted billing portal,
so no card data ever touches this app. Subscription state is delivered back via
signed webhooks (verified with STRIPE_WEBHOOK_SECRET).

`stripe` is imported lazily inside each function so importing this module never
requires the package or any credentials (tests mock these helpers). Every function
is defensive: it returns None/False on failure rather than raising, so a billing
hiccup can never take down a session.
"""

import logging

import config

logger = logging.getLogger(__name__)

PAID_PLANS = ("pro", "premium")
ALL_PLANS = ("free", "pro", "premium")


def _price_for_plan(plan: str) -> str:
    """Stripe Price ID for a paid plan, or "" if unknown/unconfigured."""
    return {
        "pro":     config.STRIPE_PRICE_PRO,        # $10 — AI analysis
        "premium": config.STRIPE_PRICE_PREMIUM,    # $25 — + recording
    }.get(plan, "")


def plan_for_price(price_id: str) -> str:
    """Reverse map a Stripe Price ID back to our plan name, or "free"."""
    if price_id and price_id == config.STRIPE_PRICE_PREMIUM:
        return "premium"
    if price_id and price_id == config.STRIPE_PRICE_PRO:
        return "pro"
    return "free"


def _init():
    """Set the Stripe API key and return the module, or None if unconfigured."""
    if not config.STRIPE_SECRET_KEY:
        return None
    try:
        import stripe
        stripe.api_key = config.STRIPE_SECRET_KEY
        return stripe
    except Exception as exc:
        logger.warning("stripe import/init failed: %s", exc)
        return None


def ensure_customer(clinician) -> "str | None":
    """Return the clinician's Stripe customer id, creating one on first use.

    Does NOT commit — the caller persists `clinician.stripe_customer_id`.
    Returns None if Stripe is unconfigured or the call fails.
    """
    if getattr(clinician, "stripe_customer_id", None):
        return clinician.stripe_customer_id
    stripe = _init()
    if stripe is None:
        return None
    try:
        customer = stripe.Customer.create(
            email=getattr(clinician, "email", None) or None,
            metadata={"clinician_id": clinician.id},
        )
        clinician.stripe_customer_id = customer.id
        return customer.id
    except Exception as exc:
        logger.warning("ensure_customer failed: %s", exc)
        return None


def create_checkout_url(clinician, plan: str, success_url: str, cancel_url: str) -> "str | None":
    """Create a Stripe Checkout (subscription) session for `plan` and return its
    URL, or None on failure. The customer is reused so plan changes/cancels show
    up under one account."""
    if plan not in PAID_PLANS:
        return None
    price = _price_for_plan(plan)
    if not price:
        return None
    stripe = _init()
    if stripe is None:
        return None
    customer_id = ensure_customer(clinician)
    try:
        sess = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=clinician.id,
            metadata={"clinician_id": clinician.id, "plan": plan},
        )
        return sess.url
    except Exception as exc:
        logger.warning("create_checkout_url failed: %s", exc)
        return None


def create_portal_url(customer_id: str, return_url: str) -> "str | None":
    """Create a Stripe billing-portal session (manage/cancel/update card)."""
    if not customer_id:
        return None
    stripe = _init()
    if stripe is None:
        return None
    try:
        sess = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=return_url,
        )
        return sess.url
    except Exception as exc:
        logger.warning("create_portal_url failed: %s", exc)
        return None


def verify_webhook(payload: bytes, sig_header: str):
    """Verify a webhook payload's signature and return the Stripe event dict, or
    None if verification fails (bad signature, unconfigured secret, tampering)."""
    if not config.STRIPE_WEBHOOK_SECRET:
        return None
    stripe = _init()
    if stripe is None:
        return None
    try:
        return stripe.Webhook.construct_event(
            payload, sig_header, config.STRIPE_WEBHOOK_SECRET,
        )
    except Exception as exc:
        logger.warning("verify_webhook failed: %s", exc)
        return None


def subscription_plan_and_status(subscription) -> "tuple[str, str]":
    """Derive (plan, status) from a Stripe Subscription object/dict. The plan
    comes from the first line item's price id; status is Stripe's own."""
    status = (subscription.get("status") if isinstance(subscription, dict)
              else getattr(subscription, "status", "")) or ""
    price_id = ""
    try:
        items = (subscription.get("items") if isinstance(subscription, dict)
                 else getattr(subscription, "items", None)) or {}
        data = items.get("data") if isinstance(items, dict) else getattr(items, "data", [])
        if data:
            first = data[0]
            price = (first.get("price") if isinstance(first, dict)
                     else getattr(first, "price", None)) or {}
            price_id = (price.get("id") if isinstance(price, dict)
                        else getattr(price, "id", "")) or ""
    except Exception:
        price_id = ""
    return plan_for_price(price_id), status
