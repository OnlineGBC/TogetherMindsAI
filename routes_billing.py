"""
routes_billing.py
-----------------
Stripe billing routes — pricing page, Checkout, hosted billing portal, and the
signed webhook. No card data ever reaches this app; subscription state arrives
via signed webhooks.

Extracted from the app monolith. The Stripe *logic* stays in billing.py (pure,
Flask-free: create_checkout_url, create_portal_url, verify_webhook,
subscription_plan_and_status) and the entitlement helpers (_effective_plan,
_has_ai_analysis, _has_recording, _session_clinician) stay in the app because
they're used app-wide. This module owns only the HTTP routes + the webhook
event-application helpers.

`register_billing_routes(app)` attaches the routes with their ORIGINAL endpoint
names (billing_page, billing_checkout, billing_portal, stripe_webhook), so every
`url_for(...)` and template link is unchanged. `billing` and `config` are used
as their own modules (the tests patch them there); other app-owned names are
looked up on the app module at request time (via `_tm`).
"""
from datetime import datetime, timezone

from flask import (session, request, redirect, url_for, render_template, flash,
                   jsonify, abort)

import billing
import config
import TogetherMindsAI as _tm


# ---------------------------------------------------------------------------
# Webhook event application — resolve the clinician and update their plan.
# ---------------------------------------------------------------------------

def _clinician_for_event_object(obj):
    """Resolve the Clinician a Stripe event object belongs to — by stored customer
    id first, falling back to client_reference_id / metadata. Backfills the
    customer id when learned from checkout."""
    cust = obj.get("customer")
    clin = _tm.Clinician.query.filter_by(stripe_customer_id=cust).first() if cust else None
    if clin is None:
        ref = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("clinician_id")
        if ref:
            clin = _tm.db.session.get(_tm.Clinician, ref)
            if clin and cust and not clin.stripe_customer_id:
                clin.stripe_customer_id = cust
    return clin


def _apply_checkout_completed(obj):
    clin = _clinician_for_event_object(obj)
    if clin is None:
        return
    # One paid plan now; what it unlocks is decided by the account's role.
    clin.plan = billing.PAID
    clin.subscription_status = "active"
    _tm.db.session.commit()
    _tm.log_event("billing_subscribed", user_id=clin.id, plan=billing.PAID)


def _apply_subscription_change(obj):
    clin = _clinician_for_event_object(obj)
    if clin is None:
        return
    plan, status = billing.subscription_plan_and_status(obj)
    clin.plan = plan
    clin.subscription_status = status
    cpe = obj.get("current_period_end")
    if cpe:
        clin.current_period_end = datetime.fromtimestamp(cpe, tz=timezone.utc)
    _tm.db.session.commit()
    _tm.log_event("billing_subscription_updated", user_id=clin.id, plan=plan, status=status)


def _apply_subscription_deleted(obj):
    clin = _clinician_for_event_object(obj)
    if clin is None:
        return
    clin.plan = "free"
    clin.subscription_status = "canceled"
    _tm.db.session.commit()
    _tm.log_event("billing_subscription_canceled", user_id=clin.id)


def register_billing_routes(app):
    """Attach the billing routes to `app`. Called once at app import."""

    @app.route("/billing")
    def billing_page():
        # Public pricing page — viewable without signing in. Personalized fields
        # stay empty for anonymous visitors; only a signed-in clinician sees
        # their own plan / renewal / manage-subscription. Subscribing still
        # requires clinician sign-in.
        import roles
        cid = _tm._current_clinician_id()
        clin = _tm.db.session.get(_tm.Clinician, cid) if cid else None
        role = roles.role_of(clin)
        # The EFFECTIVE plan, not the stored one. A comped account has no Stripe
        # subscription, so showing clin.plan told them they were on Free while
        # they in fact had everything — and offered to sell it to them again.
        paid = bool(clin) and _tm._is_paid(clin)
        comped = paid and (clin.plan or "") != billing.PAID
        return render_template(
            "billing.html",
            billing_enabled=config.BILLING_ENABLED,
            signed_in=bool(clin),
            role=role,
            role_label=roles.spec(role)["label"],
            price_label=roles.price_label(role),
            free_features=roles.free_features(role),
            paid_features=roles.paid_features(role),
            is_paid=paid,
            comped=comped,
            subscription_status=(clin.subscription_status if clin else None),
            has_customer=bool(clin and clin.stripe_customer_id),
            renews_on=(clin.current_period_end.strftime("%d %b %Y")
                       if clin and clin.current_period_end else None),
        )

    @app.route("/billing/checkout/<plan>", methods=["POST"])
    def billing_checkout(plan):
        """Start checkout for the signed-in account's own plan.

        The <plan> in the URL is ignored beyond a sanity check: price follows the
        ROLE, which is server-side. So a crafted request cannot ask to be charged
        a different amount, or buy a tier meant for someone else.
        """
        import roles
        cid = _tm._current_clinician_id()
        if not cid:
            abort(403)
        if not config.BILLING_ENABLED or plan not in billing.PAID_PLANS:
            abort(404)
        clin = _tm.db.session.get(_tm.Clinician, cid)
        role = roles.role_of(clin)
        base = url_for("billing_page", _external=True, _scheme="https")
        url = billing.create_checkout_url(clin, role, base + "?success=1", base + "?canceled=1")
        _tm.db.session.commit()                # persist any newly-created stripe_customer_id
        if not url:
            flash("Could not start checkout. Please try again.", "warning")
            return redirect(url_for("billing_page"))
        _tm.log_event("billing_checkout_started", user_id=cid, role=role)
        return redirect(url, code=303)

    @app.route("/billing/portal", methods=["POST"])
    def billing_portal():
        cid = _tm._current_clinician_id()
        if not cid:
            abort(403)
        clin = _tm.db.session.get(_tm.Clinician, cid)
        if not (clin and clin.stripe_customer_id):
            return redirect(url_for("billing_page"))
        url = billing.create_portal_url(clin.stripe_customer_id,
                                        url_for("billing_page", _external=True, _scheme="https"))
        if not url:
            flash("Could not open the billing portal. Please try again.", "warning")
            return redirect(url_for("billing_page"))
        return redirect(url, code=303)

    @app.route("/stripe/webhook", methods=["POST"])
    def stripe_webhook():
        event = billing.verify_webhook(request.get_data(), request.headers.get("Stripe-Signature", ""))
        if event is None:
            return jsonify({"error": "invalid_signature"}), 400
        etype = event.get("type", "")
        obj = (event.get("data") or {}).get("object") or {}
        try:
            if etype == "checkout.session.completed":
                _apply_checkout_completed(obj)
            elif etype in ("customer.subscription.created", "customer.subscription.updated"):
                _apply_subscription_change(obj)
            elif etype == "customer.subscription.deleted":
                _apply_subscription_deleted(obj)
        except Exception:
            _tm.db.session.rollback()
            _tm.app.logger.error("stripe webhook handling error (%s)", etype)
        return jsonify({"received": True}), 200
