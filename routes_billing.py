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

`register_billing_routes(app)` attaches the routes: pricing_page,
billing_checkout, billing_portal, stripe_webhook. `billing` and `config` are used
as their own modules (the tests patch them there); other app-owned names are
looked up on the app module at request time (via `_tm`).

The customer-facing page lives at /pricing and its endpoint is `pricing_page`.
/billing still answers, permanently redirecting — links to the old address are
already out in emails sent to real clinicians. The POST endpoints keep their
/billing/... paths: they are form actions nobody ever sees or types, so moving
them would be risk with no benefit.
"""
from datetime import datetime, timezone

from flask import (session, request, redirect, url_for, render_template, flash,
                   jsonify, abort)

import billing
import config
import TogetherMindsAI as _tm


# ---------------------------------------------------------------------------
# Which failures Stripe must deliver again.
#
# Answering 200 tells Stripe the event was dealt with, and it never comes back.
# That is right for an event whose state the NEXT event will correct, and wrong
# for money: one database hiccup and the payment is gone with nothing to show it
# ever happened.
#
# So money events answer 500 on failure and Stripe retries with backoff for about
# three days. Plan-state events keep answering 200 — a stale plan is fixed by the
# next subscription event, and retrying it forever would add noise, not safety.
#
# Replaying these is safe, which is what makes retrying them possible at all:
# hours.grant_topup is keyed on the Stripe payment, referral_from_checkout allows
# one referral per clinician, record_payment is deduped on stripe_ref, and setting
# plan to PAID twice is a no-op. The one thing a replay does repeat is the audit
# row, and a duplicate there is noise in an append-only log — deduping it would
# mean adding state to the audit path, which is worse than a repeated line.
# ---------------------------------------------------------------------------

MUST_RETRY = (
    "invoice.paid",                 # subscription money
    "checkout.session.completed",   # top-up money, the referral, and the hours
)


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


def _record_referral(obj, clin):
    """Note who referred this customer, if a partner's code was used at checkout.

    Checkout is the ONLY moment Stripe names the promotion code — the payments that
    follow name the customer and nothing else. So attribution has to be captured
    here, and the customer id stored alongside it as the link to the money.

    Never raises into the webhook: a subscription must still be applied even if
    this bookkeeping fails.
    """
    try:
        import admin_access
        promo_id = admin_access.promo_id_from_checkout(obj)
        if not promo_id:
            return
        row = admin_access.referral_from_checkout(
            _tm.db, _tm.Referral, _tm.PromoCode,
            promo_id=promo_id, clinician_id=clin.id,
            customer_id=obj.get("customer"),
            now=datetime.now(timezone.utc),
        )
        if row is not None:
            # The code and the share, not the partner's name or the customer's:
            # the audit log takes metadata, never PII.
            _tm.log_event("referral_recorded", user_id=clin.id,
                          code=row.code, commission_pct=row.commission_pct)
    except Exception:
        _tm.db.session.rollback()
        _tm.app.logger.warning("referral not recorded for checkout")


def _record_collected_payment(*, customer_id, stripe_ref, amount_cents, currency,
                              when):
    """Record money Stripe collected from a referred customer.

    The one funnel for both kinds of payment, so the rules that decide what a
    partner earns — the duplicate guard, the one-year window, the share stored at
    the time — live in exactly one place and cannot drift apart.

    `when` is a Unix timestamp from Stripe, or None.
    """
    import admin_access
    row = admin_access.record_payment(
        _tm.db, _tm.Referral, _tm.ReferralPayment,
        customer_id=customer_id, stripe_ref=stripe_ref,
        amount_cents=amount_cents, currency=currency,
        paid_at=(datetime.fromtimestamp(when, tz=timezone.utc) if when
                 else datetime.now(timezone.utc)),
    )
    if row is not None:
        _tm.log_event("referral_payment_recorded", referral_id=row.referral_id,
                      amount_cents=row.amount_cents,
                      commission_cents=row.commission_cents,
                      in_window=row.in_window)
    return row


# Whether this process has already emailed about an unreadable discount.
#
# Once the payload shape breaks it breaks for EVERY invoice, so the first email
# says everything the tenth would. Without this you would get one per renewal, all
# identical.
#
# A module flag rather than a stored one, deliberately: no new column, and it
# resets when the process restarts, so a deploy that has not fixed the problem
# raises it again. Being told twice is the point — being told once and missing it
# is how this goes quiet. Cloud Run runs several instances, so expect a handful
# rather than exactly one.
_unreadable_discount_emailed = False


def _warn_discount_unreadable():
    """Tell the admins the payout report has stopped being able to attribute.

    Never raises: this runs after the money is already recorded, and a mail
    problem must not turn a successful payment into a failed webhook.
    """
    global _unreadable_discount_emailed
    if _unreadable_discount_emailed:
        return
    to = [a for a in (config.ADMIN_EMAILS or []) if a]
    if not to:
        return
    subject = "TogetherMindsAI — partner payouts cannot read discount codes"
    body = (
        "A customer paid an invoice with a discount on it, but Stripe did not "
        "tell us which discount code was used.\n\n"
        "What this means: new sign-ups through a partner's code may not be "
        "recorded, so those partners would earn nothing and the payout report "
        "would look empty rather than wrong.\n\n"
        "What to check: the Stripe webhook endpoint's API version. The field that "
        "carries the code was replaced in newer versions. If the endpoint was "
        "recreated recently it will have moved to a newer version on its own.\n\n"
        "Payments are still being recorded. It is only the link to the partner "
        "that is missing.\n")
    try:
        _tm._send_email(to, subject, body,
                        "<p>" + body.replace("\n\n", "</p><p>") + "</p>")
        _unreadable_discount_emailed = True
    except Exception:
        # Not marked as sent, so the next invoice tries again.
        _tm.app.logger.warning("could not email admins about unreadable discount")


def _attribute_from_invoice(obj):
    """Attribute the referral from the invoice itself, if checkout has not yet.

    Stripe creates checkout.session.completed and invoice.paid in the same instant
    and does not promise which is DELIVERED first. It really does go both ways —
    of this account's two live sign-ups, one arrived in each order. When the
    invoice came first there was no referral to attach the money to, so the first
    payment was dropped, and because the webhook answers 200 either way Stripe
    never retried it.

    The invoice names the promotion code too, so attributing from here as well
    makes the order stop mattering. Whichever event lands first does the work; the
    other finds it already done.

    Returns True when a discount was applied but no code could be read, so the
    caller can raise the alarm AFTER the money is safely recorded.

    Never raises: recording the payment matters more than tidy attribution.
    """
    try:
        import admin_access
        promo_id = admin_access.promo_id_from_invoice(obj)
        if not promo_id:
            if admin_access.discount_unreadable(obj):
                # Warning, not info: info does not reach Cloud Run's logs, and a
                # discount we cannot read is how this report goes quiet without
                # anything appearing to be broken. See discount_unreadable.
                _tm.app.logger.warning(
                    "invoice discount carries no readable promotion code — "
                    "payout attribution from the invoice is not working")
                return True
            return False
        row = admin_access.referral_from_invoice(
            _tm.db, _tm.Referral, _tm.PromoCode, _tm.Clinician,
            promo_id=promo_id, customer_id=obj.get("customer"),
            now=datetime.now(timezone.utc))
        if row is not None:
            _tm.log_event("referral_recorded", user_id=row.clinician_id,
                          code=row.code, commission_pct=row.commission_pct,
                          source="invoice")
    except Exception:
        _tm.db.session.rollback()
        _tm.app.logger.warning("referral not recorded from invoice")
    return False


def _apply_invoice_paid(obj):
    """Record a SUBSCRIPTION payment Stripe collected.

    The amount is taken as Stripe reports it — `amount_paid`, what actually
    arrived after the discount. Working it out from a list price would overpay
    every partner, every month.

    Fires on renewals as well as the first charge, which is the point: a partner
    earns for a year, so every payment in that year has to be seen.
    """
    # Attribution FIRST, so a first payment that overtook the checkout event still
    # has a referral to attach itself to.
    unreadable = _attribute_from_invoice(obj)
    # `or {}` rather than a dict default: Stripe sends the key with a null value,
    # and a default would not save us from calling .get on None.
    when = (obj.get("status_transitions") or {}).get("paid_at") or obj.get("created")
    _record_collected_payment(
        customer_id=obj.get("customer"), stripe_ref=obj.get("id"),
        amount_cents=obj.get("amount_paid"), currency=obj.get("currency"),
        when=when)
    # LAST, and only after the money is safely recorded. Sending mail is slow and
    # can fail, and a failure here now means Stripe redelivers the payment — so
    # this must never sit in front of the thing it is warning about.
    if unreadable:
        _warn_discount_unreadable()


def _record_topup_payment(obj, clin):
    """Record a one-off hours purchase as money collected from a referral.

    A top-up is mode="payment", so Stripe creates no invoice and invoice.paid
    never fires — the checkout session is the only place this money appears.
    `amount_total` is what the customer paid after the discount came off.

    Called ONLY from the top-up branch below, and that is what stops a
    subscription being counted twice: a subscription checkout never reaches here,
    so its money can only ever arrive via invoice.paid.

    Never raises: the hours have already been granted, and a bookkeeping failure
    must not cost someone the time they just bought.
    """
    try:
        _record_collected_payment(
            customer_id=obj.get("customer"),
            # The same reference hours.grant_topup keys on, so both agree on what
            # "this payment" means.
            stripe_ref=obj.get("payment_intent") or obj.get("id"),
            amount_cents=obj.get("amount_total"),
            currency=obj.get("currency"),
            when=obj.get("created"))
    except Exception:
        _tm.db.session.rollback()
        _tm.app.logger.warning("top-up payment not recorded for payout")


def _apply_checkout_completed(obj):
    clin = _clinician_for_event_object(obj)
    if clin is None:
        return
    # Before the early return below: a top-up bought with a partner's code is
    # still that partner's referral.
    _record_referral(obj, clin)
    # A top-up is a ONE-OFF purchase of recording hours. It must not be mistaken
    # for a subscription — otherwise buying extra hours would quietly put someone
    # on a monthly plan they never asked for.
    if (obj.get("metadata") or {}).get("kind") == billing.TOPUP_KIND:
        import hours
        # Keyed on the payment so a webhook delivered twice cannot credit twice.
        ref = obj.get("payment_intent") or obj.get("id")
        granted = hours.grant_topup(_tm.db, _tm.HoursGrant, clin.id, stripe_ref=ref)
        _tm.log_event("recording_hours_purchased", user_id=clin.id,
                      minutes=(granted.minutes if granted else 0),
                      duplicate=granted is None)
        # Hours first, payout second: the purchase is what they paid for.
        _record_topup_payment(obj, clin)
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
    def billing_page_moved():
        """The page's old address. Kept because links to it are already OUT THERE:
        recording-retention emails sent to real clinicians carry a /billing link
        (see _recording_retention_sweep), and those must not land on a 404 months
        later. 301 so browsers and search engines learn the new one.

        The query string is carried across. Any Stripe Checkout created before this
        moved has a success_url of /billing?success=1, so dropping it would send
        those customers to a page that never says their payment worked.
        """
        target = url_for("pricing_page")
        query = request.query_string.decode()
        return redirect(target + ("?" + query if query else ""), code=301)

    @app.route("/pricing")
    def pricing_page():
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
        # Recording time, for the roles that are metered. None means "not metered",
        # which is what the template keys on to hide the whole block.
        hours_left = None
        if clin is not None and _tm._hours_metered(clin) and paid:
            import hours as _hours
            hours_left = _hours.describe(_tm._recording_minutes_left(clin))
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
            hours_left=hours_left,
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
        base = url_for("pricing_page", _external=True, _scheme="https")
        url = billing.create_checkout_url(clin, role, base + "?success=1", base + "?canceled=1")
        _tm.db.session.commit()                # persist any newly-created stripe_customer_id
        if not url:
            flash("Could not start checkout. Please try again.", "warning")
            return redirect(url_for("pricing_page"))
        _tm.log_event("billing_checkout_started", user_id=cid, role=role)
        return redirect(url, code=303)

    @app.route("/billing/topup", methods=["POST"])
    def billing_topup():
        """Buy another 40 recording hours — a single charge, not a subscription.

        Caregivers only: nobody else is metered, so nobody else has hours to buy.
        """
        import roles
        cid = _tm._current_clinician_id()
        if not cid:
            abort(403)
        clin = _tm.db.session.get(_tm.Clinician, cid)
        if not config.BILLING_ENABLED or roles.role_of(clin) != roles.CAREGIVER:
            abort(404)
        base = url_for("pricing_page", _external=True, _scheme="https")
        url = billing.create_topup_checkout_url(
            clin, base + "?hours=1", base + "?canceled=1")
        _tm.db.session.commit()          # persist any newly-created customer id
        if not url:
            flash("Could not start checkout. Please try again.", "warning")
            return redirect(url_for("pricing_page"))
        _tm.log_event("hours_topup_started", user_id=cid)
        return redirect(url, code=303)

    @app.route("/billing/portal", methods=["POST"])
    def billing_portal():
        cid = _tm._current_clinician_id()
        if not cid:
            abort(403)
        clin = _tm.db.session.get(_tm.Clinician, cid)
        if not (clin and clin.stripe_customer_id):
            return redirect(url_for("pricing_page"))
        url = billing.create_portal_url(clin.stripe_customer_id,
                                        url_for("pricing_page", _external=True, _scheme="https"))
        if not url:
            flash("Could not open the billing portal. Please try again.", "warning")
            return redirect(url_for("pricing_page"))
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
            # Where the partner payout numbers come from. Fires on renewals too,
            # which is the point: a partner earns for a year, so every payment
            # collected in that year has to be seen.
            elif etype == "invoice.paid":
                _apply_invoice_paid(obj)
        except Exception:
            _tm.db.session.rollback()
            _tm.app.logger.error("stripe webhook handling error (%s)", etype)
            if etype in MUST_RETRY:
                # 500 so Stripe delivers it again. Answering 200 here told Stripe
                # the money had been dealt with when it had not, and it never came
                # back — one database hiccup and that payment was gone for good.
                return jsonify({"error": "retry"}), 500
        return jsonify({"received": True}), 200
