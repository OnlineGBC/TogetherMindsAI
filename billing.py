"""
billing.py
----------
Phase 4 Step 4 — clinician subscription billing via Stripe.

Three tiers (see config): free / pro ($10, AI analysis) / premium ($25, + recording).
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

# There is now ONE paid plan. Which features it unlocks is decided by the
# account's role, not by which tier they bought (see roles.py). "pro" and
# "premium" are retired and only appear when reading an old stored value.
PAID = "paid"
FREE = "free"
PAID_PLANS = (PAID,)
ALL_PLANS = (FREE, PAID)
RETIRED_PLANS = ("pro", "premium")


def price_for_role(role: str) -> str:
    """Stripe Price ID that sells this role's paid plan, or "" if unconfigured."""
    import roles
    return getattr(config, roles.price_key(role), "") or ""


def plan_for_price(price_id: str) -> str:
    """Reverse map a Stripe Price ID to a plan name.

    Any price we currently sell means "paid". The retired Pro/Premium prices map
    to free: those tiers no longer exist, so an old subscription must not keep
    granting access under a plan name nothing recognises.
    """
    if not price_id:
        return FREE
    if price_id in (config.STRIPE_PRICE_CLINICAL, config.STRIPE_PRICE_CAREGIVER):
        return PAID
    return FREE


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


def create_checkout_url(clinician, role: str, success_url: str, cancel_url: str) -> "str | None":
    """Create a Stripe Checkout (subscription) session for this ROLE's paid plan
    and return its URL, or None on failure.

    The price follows the role rather than a chosen tier, so there is nothing for
    the caller to pick — and nothing a crafted request can ask to be charged.
    The customer is reused so changes and cancels show under one account.
    """
    import roles
    if not roles.is_valid(role):
        return None
    price = price_for_role(role)
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
            # Shows the "Add promotion code" box. Without it a Stripe coupon has
            # nowhere to be typed, so a 100%-off code cannot be used to test the
            # live payment path without really paying.
            allow_promotion_codes=True,
            metadata={"clinician_id": clinician.id, "plan": PAID, "role": role},
        )
        return sess.url
    except Exception as exc:
        logger.warning("create_checkout_url failed: %s", exc)
        return None


TOPUP_KIND = "hours_topup"


def create_topup_checkout_url(clinician, success_url: str, cancel_url: str) -> "str | None":
    """Create a ONE-TIME Stripe Checkout for another 40 recording hours.

    mode="payment", not "subscription": buying extra hours must not sign anyone
    up to a repeating charge. The metadata marks it so the webhook credits hours
    instead of granting a plan.
    """
    price = config.STRIPE_PRICE_HOURS_TOPUP
    if not price:
        return None
    stripe = _init()
    if stripe is None:
        return None
    customer_id = ensure_customer(clinician)
    try:
        sess = stripe.checkout.Session.create(
            mode="payment",
            customer=customer_id,
            line_items=[{"price": price, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=clinician.id,
            # Same code box as the subscription checkout, so buying hours can be
            # tested without really paying either.
            allow_promotion_codes=True,
            metadata={"clinician_id": clinician.id, "kind": TOPUP_KIND},
        )
        return sess.url
    except Exception as exc:
        logger.warning("create_topup_checkout_url failed: %s", exc)
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
    """Verify a webhook payload's signature and return the event as a PLAIN dict,
    or None if verification fails (bad signature, unconfigured secret, tampering).

    construct_event returns a Stripe `StripeObject`, which does NOT support dict
    `.get()` — so after verifying the signature we return the parsed JSON instead,
    giving callers ordinary dict semantics."""
    if not config.STRIPE_WEBHOOK_SECRET:
        return None
    stripe = _init()
    if stripe is None:
        return None
    try:
        stripe.Webhook.construct_event(payload, sig_header, config.STRIPE_WEBHOOK_SECRET)
    except Exception as exc:
        logger.warning("verify_webhook failed: %s", exc)
        return None
    try:
        import json
        return json.loads(payload)
    except Exception as exc:
        logger.warning("verify_webhook payload parse failed: %s", exc)
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


# ---------------------------------------------------------------------------
# The one discount code the admin console manages.
#
# Stripe will not rename a promotion code — its update endpoint accepts only
# `active`, `metadata` and `restrictions`. So changing the code means creating a
# new promotion code on the same coupon and switching the old one off. That is
# two calls, done here so the console shows one field and one button.
#
# Unlike the rest of this module these raise on failure rather than returning
# None. A half-done change must be visible: the caller shows the error and keeps
# the stored code exactly as it was.
# ---------------------------------------------------------------------------

# Fixed id, so the coupon is found rather than duplicated on every call.
COUPON_ID = "tmai_admin_100off"
DEFAULT_DISCOUNT_CODE = "100-0ffTh1sBuy"


def _stripe_or_raise():
    stripe = _init()
    if stripe is None:
        raise RuntimeError("Stripe is not configured.")
    return stripe


def ensure_coupon():
    """The 100%-off coupon, created once if it is not there yet."""
    stripe = _stripe_or_raise()
    try:
        return stripe.Coupon.retrieve(COUPON_ID)
    except Exception:
        return stripe.Coupon.create(
            id=COUPON_ID, percent_off=100, duration="forever",
            name="TogetherMindsAI admin discount",
        )


def create_promotion_code(code: str):
    """Create the promotion code customers type. Returns the Stripe object."""
    stripe = _stripe_or_raise()
    ensure_coupon()
    # stripe.PromotionCode, not stripe.promotion_codes: the snake_case accessors
    # live on a StripeClient instance, not on the module. Calling the wrong one
    # raised AttributeError, which the console reported as "the code may already
    # be in use" — a guess that sent the diagnosis the wrong way.
    return stripe.PromotionCode.create(
        promotion={"type": "coupon", "coupon": COUPON_ID}, code=code)


def deactivate_promotion_code(promo_id: str) -> None:
    """Switch an old promotion code off. Missing or already-off is not an error."""
    if not promo_id:
        return
    stripe = _stripe_or_raise()
    try:
        stripe.PromotionCode.modify(promo_id, active=False)
    except Exception as exc:
        logger.warning("could not deactivate promotion code %s: %s", promo_id, exc)


def promotion_code_uses(promo_id: str) -> "int | None":
    """How many times this code has been redeemed, or None if unknown."""
    if not promo_id:
        return None
    try:
        stripe = _stripe_or_raise()
        return stripe.PromotionCode.retrieve(promo_id).times_redeemed
    except Exception as exc:
        logger.warning("could not read promotion code %s: %s", promo_id, exc)
        return None


# Stripe rejects anything else: "Valid characters are lower case letters (a-z),
# upper case letters (A-Z), digits (0-9), and dashes (-)."
def is_valid_code(code: str) -> bool:
    import re
    return bool(code) and len(code) <= 64 and re.fullmatch(r"[A-Za-z0-9-]+", code) is not None


# ---------------------------------------------------------------------------
# Partner referral codes.
#
# One Stripe coupon per DISCOUNT LEVEL, shared by every partner offering that
# level — a coupon is only a discount definition. The promotion CODE is what is
# unique per partner, and it doubles as the attribution link: a checkout that
# used this code is this partner's referral.
#
# Only the discount exists in Stripe. The partner's commission is ours alone and
# never leaves our database (see models.Partner).
# ---------------------------------------------------------------------------

# Stripe's own rule: "Valid characters are lower case letters (a-z), upper case
# letters (A-Z), digits (0-9), and dashes (-)."
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no O/0, I/1/L — misread aloud


def partner_coupon_id(discount_pct: int) -> str:
    return f"tmai_partner_{int(discount_pct)}off"


def ensure_partner_coupon(discount_pct: int):
    """The coupon for this discount level, created once and reused after that."""
    stripe = _stripe_or_raise()
    cid = partner_coupon_id(discount_pct)
    try:
        return stripe.Coupon.retrieve(cid)
    except Exception:
        return stripe.Coupon.create(
            id=cid, percent_off=int(discount_pct), duration="forever",
            name=f"TogetherMindsAI partner {int(discount_pct)}% off",
        )


def suggest_partner_code(name: str) -> str:
    """NAME-XXXX. The random tail is the point: without it, someone holding
    EASTON10 would simply try EASTON50 and take the bigger discount."""
    import re
    import secrets
    base = re.sub(r"[^A-Za-z0-9]", "", (name or "").strip()).upper()[:16] or "PARTNER"
    tail = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
    return f"{base}-{tail}"


def create_partner_code(code: str, discount_pct: int, max_uses=None):
    """Create the partner's promotion code in Stripe. Returns the Stripe object.

    max_redemptions is enforced by STRIPE, not by us — a cap we counted ourselves
    could be walked past by two people checking out at the same moment.
    """
    stripe = _stripe_or_raise()
    ensure_partner_coupon(discount_pct)
    kwargs = {
        "promotion": {"type": "coupon", "coupon": partner_coupon_id(discount_pct)},
        "code": code,
    }
    if max_uses:
        kwargs["max_redemptions"] = int(max_uses)
    return stripe.PromotionCode.create(**kwargs)
