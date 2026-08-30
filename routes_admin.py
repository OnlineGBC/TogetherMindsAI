"""
routes_admin.py
---------------
The comp-access console at /accessadmin — grant an email full access without
paying, and revoke it.

Reached only by an address listed in ADMIN_EMAILS, and only after a second
factor — either an authenticator-app code or a code emailed to the admin.
Anyone else — signed out, signed in as a normal clinician, or hitting the URL
cold — gets a 404, so the console's existence is not advertised.

`register_admin_routes(app)` attaches the routes, matching how routes_billing /
routes_oauth are wired. The rules live in admin_access.py (Flask-free); this
module owns only the HTTP layer.
"""
from datetime import datetime, timezone, timedelta

from flask import (session, request, redirect, url_for, render_template, flash,
                   abort)

import admin_access
import config
import roles
import TogetherMindsAI as _tm

_VERIFIED_UNTIL = "_admin_verified_until"


def _current_admin_email():
    """The signed-in clinician's email if they are an admin, else None."""
    if not config.ADMIN_CONSOLE_ENABLED:
        return None
    uid = session.get("user_id")
    if not uid:
        return None
    clin = _tm.db.session.get(_tm.Clinician, uid)
    email = getattr(clin, "email", None) if clin else None
    return email if admin_access.is_admin(email or "") else None


def _require_admin():
    """The admin's email, or 404. A non-admin must not be able to tell the
    difference between "not allowed" and "no such page"."""
    email = _current_admin_email()
    if not email:
        abort(404)
    return email


def _is_verified() -> bool:
    raw = session.get(_VERIFIED_UNTIL)
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw) > datetime.now(timezone.utc)
    except ValueError:
        return False


def _mark_verified() -> None:
    until = datetime.now(timezone.utc) + timedelta(minutes=config.ADMIN_SESSION_MINUTES)
    session[_VERIFIED_UNTIL] = until.isoformat()


def _codes_view() -> dict:
    """Template values for the Discount codes card.

    One card for every code, with or without a partner behind it. Hidden when
    billing is off: with no checkout, a code has nowhere to be typed.
    """
    if not config.BILLING_ENABLED:
        return {"codes_enabled": False, "promo_codes": [], "promo_uses": {}}
    return {
        "codes_enabled": True,
        "promo_codes": admin_access.list_promo_codes(_tm.PromoCode),
        # Read from Stripe, which is the only place that actually counts them.
        "promo_uses": admin_access.promo_code_uses(_tm.PromoCode),
    }


def _audit_view() -> dict:
    """Template values for the Recent activity card.

    The filters come from the query string, so a useful view can be bookmarked
    and shared with the next admin rather than rebuilt by hand each time.
    """
    filters = {
        "who": (request.args.get("who") or "").strip(),
        "event": (request.args.get("event") or "").strip(),
        "date_from": (request.args.get("from") or "").strip(),
        "date_to": (request.args.get("to") or "").strip(),
        "text": (request.args.get("q") or "").strip(),
    }
    # Everything, not just admin actions, when the box is ticked. The rest of the
    # log is session traffic and is noise on this page by default.
    admin_only = request.args.get("all") != "1"
    rows, truncated = admin_access.search_audit(
        _tm.db, _tm.AuditLog, _tm.Clinician, admin_only=admin_only, **filters)
    return {
        "audit_rows": rows,
        "audit_truncated": truncated,
        "audit_filters": filters,
        "audit_admin_only": admin_only,
        "audit_event_types": admin_access.audit_event_types(_tm.AuditLog),
        "audit_labels": admin_access.account_labels(_tm.Clinician),
        "audit_limit": admin_access.AUDIT_PAGE_LIMIT,
    }


def register_admin_routes(app):

    @app.route("/accessadmin")
    def admin_access_page():
        email = _require_admin()
        if not _is_verified():
            return render_template(
                "admin_access.html", verified=False, grants=[], admin_email=email,
                totp_available=bool(config.ADMIN_TOTP_SECRET),
                factors_required=config.ADMIN_FACTORS_REQUIRED,
            )
        accounts, total = admin_access.list_accounts(_tm.Clinician)
        return render_template(
            "admin_access.html", verified=True, admin_email=email,
            grants=admin_access.active_grants(_tm.CompAccess),
            accounts=accounts, account_total=total,
            account_limit=admin_access.ACCOUNT_LIST_LIMIT,
            role_choices=roles.choices(), role_of=roles.role_of,
            disable_notice=admin_access.DISABLE_NOTICE,
            self_id=session.get("user_id"),
            admin_emails=[a.lower() for a in (config.ADMIN_EMAILS or [])],
            **_codes_view(),
            **_audit_view(),
            totp_available=bool(config.ADMIN_TOTP_SECRET),
            factors_required=config.ADMIN_FACTORS_REQUIRED,
        )

    @app.route("/accessadmin/send-code", methods=["POST"])
    def admin_access_send_code():
        """Issue and deliver a one-time code on the requested channel."""
        email = _require_admin()
        channel = (request.form.get("channel") or "").strip()
        if channel not in admin_access.CHANNELS:
            abort(400)

        code = admin_access.issue_code(_tm.db, _tm.AdminAuthCode, email, channel)
        sent = _send_code_email(email, code)
        _tm.log_event("admin_code_sent", user_id=session.get("user_id"),
                      channel=channel, delivered=bool(sent))
        flash("Code sent." if sent else
              "Could not send that code — use your authenticator app instead.", "info")
        return redirect(url_for("admin_access_page"))

    @app.route("/accessadmin/verify", methods=["POST"])
    def admin_access_verify():
        """Check the submitted factors; either one is enough by default."""
        email = _require_admin()
        passed = admin_access.count_factors(
            _tm.db, _tm.AdminAuthCode, email,
            totp=request.form.get("totp", ""),
            email_code=request.form.get("email_code", ""),
        )
        ok = passed >= config.ADMIN_FACTORS_REQUIRED
        _tm.log_event("admin_challenge", user_id=session.get("user_id"),
                      factors_passed=passed, granted=ok)
        if ok:
            _mark_verified()
        else:
            flash(f"{passed} of {config.ADMIN_FACTORS_REQUIRED} required factors "
                  "verified. Codes are single-use — request fresh ones.", "danger")
        return redirect(url_for("admin_access_page"))

    @app.route("/accessadmin/add", methods=["POST"])
    def admin_access_add():
        email = _require_admin()
        if not _is_verified():
            abort(403)
        target = (request.form.get("email") or "").strip()
        row = admin_access.grant(_tm.db, _tm.CompAccess, target,
                                 request.form.get("note", ""), email)
        if row is None:
            flash("That does not look like an email address.", "danger")
        else:
            _tm.log_event("comp_access_granted", user_id=session.get("user_id"),
                          comp_id=row.id)
            flash(f"{target} now has full access.", "info")
        return redirect(url_for("admin_access_page"))

    @app.route("/accessadmin/role", methods=["POST"])
    def admin_access_set_role():
        """Change an account's role — admin only, deliberately not self-serve.

        A role decides what the app may claim and store about someone's work, so
        moving one is a real change: it can take away ICD codes, or remove the
        state licence gate from their sessions.
        """
        _require_admin()
        if not _is_verified():
            abort(403)
        target = (request.form.get("clinician_id") or "").strip()
        new_role = (request.form.get("role") or "").strip()
        changed = admin_access.set_role(_tm.db, _tm.Clinician, target, new_role)
        if changed is None:
            flash("No change made — unknown account, or already that role.", "danger")
        else:
            old_role, set_to = changed
            _tm.log_event("role_changed", user_id=session.get("user_id"),
                          target_id=target, old_role=old_role, new_role=set_to)
            flash(f"Role changed to {roles.spec(set_to)['label']}.", "info")
        return redirect(url_for("admin_access_page"))

    @app.route("/accessadmin/disable", methods=["POST"])
    def admin_access_set_disabled():
        """Switch an account off, or back on — admin only.

        Reversible by design: it takes away access and destroys nothing, which is
        why there is no confirmation step. Deleting the account instead would
        orphan the therapy sessions and licence certificates that carry its id,
        and those are client records the practice has to keep.
        """
        _require_admin()
        if not _is_verified():
            abort(403)
        target = (request.form.get("clinician_id") or "").strip()
        want_disabled = request.form.get("disabled") == "1"
        # Switching yourself off would lock you out of this console, with no way
        # back in — nobody else can undo it for you.
        if target and target == session.get("user_id"):
            flash("You cannot disable your own account.", "danger")
            return redirect(url_for("admin_access_page"))
        changed = admin_access.set_disabled(_tm.db, _tm.Clinician, target, want_disabled)
        if changed is None:
            flash("No change made — unknown account, or already in that state.", "danger")
        else:
            _tm.log_event("account_disabled" if changed else "account_enabled",
                          user_id=session.get("user_id"), target_id=target,
                          notice=admin_access.DISABLE_NOTICE)
            flash("Account disabled — they cannot sign in until you enable it."
                  if changed else
                  "Account enabled — they can sign in again.", "info")
        return redirect(url_for("admin_access_page"))

    @app.route("/accessadmin/code", methods=["POST"])
    def admin_access_promo_code():
        """Add, edit or delete a discount code. Admin only.

        Nothing is stored unless Stripe accepted the code: a row here with no
        code there would hand someone a code that does not work.
        """
        email = _require_admin()
        if not _is_verified():
            abort(403)
        edit_id = (request.form.get("edit_id") or "").strip()
        delete_id = (request.form.get("delete_id") or "").strip()
        try:
            if delete_id:
                if admin_access.delete_promo_code(_tm.db, _tm.PromoCode, int(delete_id)):
                    _tm.log_event("promo_code_deleted", user_id=session.get("user_id"),
                                  row_id=int(delete_id))
                    flash("Code deleted. It no longer works.", "info")
                else:
                    flash("No change made — that code is already gone.", "danger")
            elif edit_id:
                row = admin_access.edit_promo_code(
                    _tm.db, _tm.PromoCode, int(edit_id),
                    label=request.form.get("label", ""),
                    email=request.form.get("email", ""),
                    commission_pct=request.form.get("commission_pct", ""),
                    # An unticked box sends nothing at all, which is how a
                    # checkbox says "off".
                    active=(request.form.get("active") == "1"),
                )
                if row is None:
                    flash("No change made — unknown code.", "danger")
                else:
                    _tm.log_event("promo_code_edited", user_id=session.get("user_id"),
                                  code=row.code, commission_pct=row.commission_pct,
                                  active=row.active)
                    flash(f"Saved {row.code}.", "info")
            else:
                row = admin_access.create_promo_code(
                    _tm.db, _tm.PromoCode,
                    label=request.form.get("label", ""),
                    email=request.form.get("email", ""),
                    discount_pct=request.form.get("discount_pct", ""),
                    commission_pct=request.form.get("commission_pct", ""),
                    max_uses=request.form.get("max_uses", ""),
                    admin_email=email,
                )
                # The code, not the name: it is the thing that has to be passed on.
                _tm.log_event("promo_code_added", user_id=session.get("user_id"),
                              code=row.code, discount_pct=row.discount_pct,
                              commission_pct=row.commission_pct)
                flash(f"Code created: {row.code}", "info")
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            app.logger.warning("promo code change failed: %s: %s", type(exc).__name__, exc)
            flash(f"Nothing was saved. Stripe said: {exc}", "danger")
        return redirect(url_for("admin_access_page"))

    @app.route("/accessadmin/revoke", methods=["POST"])
    def admin_access_revoke():
        _require_admin()
        if not _is_verified():
            abort(403)
        try:
            row_id = int(request.form.get("id", ""))
        except ValueError:
            abort(400)
        if admin_access.revoke(_tm.db, _tm.CompAccess, row_id):
            _tm.log_event("comp_access_revoked", user_id=session.get("user_id"),
                          comp_id=row_id)
            flash("Access revoked.", "info")
        return redirect(url_for("admin_access_page"))


def _send_code_email(to_email: str, code: str) -> bool:
    """Email the admin a one-time code. Never raises."""
    try:
        subject = "TogetherMindsAI — admin sign-in code"
        plain = (f"Your admin sign-in code is {code}.\n\n"
                 f"It expires in {config.ADMIN_CODE_TTL_MINUTES} minutes and can be "
                 "used once. If you did not request it, someone has your sign-in — "
                 "change your password.\n")
        html = ('<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;">'
                '<p>Your admin sign-in code is</p>'
                f'<p style="font-size:28px;font-weight:700;letter-spacing:4px;">{code}</p>'
                f'<p style="color:#555;">It expires in {config.ADMIN_CODE_TTL_MINUTES} '
                'minutes and can be used once. If you did not request it, someone has '
                'your sign-in — change your password.</p></div>')
        _tm._send_email([to_email], subject, plain, html)
        return True
    except Exception:
        _tm.app.logger.warning("admin code email failed")
        return False
