"""
routes_oauth.py
---------------
Clinician & client OAuth (OpenID Connect) login routes — Google & Microsoft.

Extracted from the app monolith. The OAuth *plumbing* (the `oauth` object, the
network-exchange helpers `_oauth_start`/`_oauth_userinfo`/`_oauth_subject`, the
Microsoft issuer validator, the open-redirect guard `_safe_next`, and the
session readers) stays in TogetherMindsAI.py — it is shared with the rest of the
app and the tests patch it there. This module owns only the route handlers:
which account to create and where to send the user.

`register_oauth_routes(app)` attaches the routes with their ORIGINAL endpoint
names (login, oauth_login, oauth_callback, logout, client_login,
client_oauth_login, client_oauth_callback), so every existing `url_for(...)`
and template link keeps working unchanged. Shared names are looked up on the
app module at request time (via `_tm`), so existing test patches
(`patch.object(tm.oauth, ...)`, `patch.object(tm, "_oauth_start")`, …) still
take effect.
"""
import uuid
from datetime import datetime, timezone

from flask import session, request, redirect, url_for, render_template, flash

import TogetherMindsAI as _tm


def register_oauth_routes(app):
    """Attach the OAuth login routes to `app`. Called once at app import."""

    @app.route("/login")
    def login():
        """Clinician login page — Sign in with Google / Microsoft."""
        if _tm._current_clinician_id():
            return redirect(url_for("therapist_start"))
        # Carry a safe return target (e.g. a download link opened on a phone)
        # through to the sign-in buttons so we can come back to it after login.
        return render_template("login.html", next=_tm._safe_next(request.args.get("next")))

    # ---- Clinician OAuth routes -------------------------------------------

    @app.route("/auth/<provider>/login")
    def oauth_login(provider):
        # Stash the post-login return target HERE — the same request where
        # Authlib writes its OAuth state — so it survives the provider round-trip
        # reliably (a session set earlier can be dropped by mobile in-app
        # browsers). Clinicians grant "email" so we can send them their own
        # recording links + retention notices; the client flow stays "openid".
        nxt = _tm._safe_next(request.args.get("next"))
        if nxt:
            session["post_login_next"] = nxt
        return _tm._oauth_start(provider, "oauth_callback", scope="openid email")

    @app.route("/auth/<provider>/callback")
    def oauth_callback(provider):
        if provider not in _tm._OAUTH_PROVIDERS:
            return _tm._redirect_invalid_session()
        info = _tm._oauth_userinfo(provider)
        subject = info.get("sub") if info else None
        if not subject:
            flash("Sign-in did not complete. Please try again.", "warning")
            return redirect(url_for("login"))
        email = (info.get("email") or "").strip().lower() or None

        now = datetime.now(timezone.utc)
        clinician = (
            _tm.Clinician.query
            .filter_by(provider=provider, provider_subject=subject)
            .first()
        )
        if clinician is None:
            clinician = _tm.Clinician(
                id=str(uuid.uuid4()), provider=provider, provider_subject=subject,
                email=email, created_at=now, last_login_at=now,
            )
            _tm.db.session.add(clinician)
            _tm.log_event("clinician_registered", user_id=clinician.id, provider=provider)
        else:
            clinician.last_login_at = now
            if email and clinician.email != email:
                clinician.email = email   # backfill / keep current for existing accounts
        _tm.db.session.commit()

        # Regenerate the session on this privilege change to defeat session
        # fixation: preserve only the post-login redirect, drop anything an
        # attacker could have pre-seeded, then establish the authenticated
        # identity. The clinician's account id is their identity everywhere.
        nxt = _tm._safe_next(session.pop("post_login_next", None))
        session.clear()
        session["user_id"]      = clinician.id
        session["clinician_id"] = clinician.id
        session.permanent = True
        _tm.log_event("clinician_login", user_id=clinician.id, provider=provider)
        # Return to where they came from (e.g. a download link), else the dashboard.
        return redirect(nxt or url_for("therapist_start"))

    @app.route("/logout")
    def logout():
        cid = _tm._current_clinician_id()
        if cid:
            _tm.log_event("clinician_logout", user_id=cid)
        client_id = _tm._current_client_account_id()
        if client_id:
            _tm.log_event("client_logout", user_id=client_id)
        session.clear()
        return redirect(url_for("welcome"))

    # ---- Client OAuth routes ----------------------------------------------

    @app.route("/client/login")
    def client_login():
        """Optional client sign-in page — Google / Microsoft."""
        if _tm._current_client_account_id():
            return redirect(url_for("my_sessions"))
        # Optionally remember where to return after login (e.g. a session URL).
        nxt = _tm._safe_next(request.args.get("next"))
        if nxt:
            session["client_login_next"] = nxt
        return render_template("client_login.html")

    @app.route("/client/auth/<provider>/login")
    def client_oauth_login(provider):
        return _tm._oauth_start(provider, "client_oauth_callback")

    @app.route("/client/auth/<provider>/callback")
    def client_oauth_callback(provider):
        if provider not in _tm._OAUTH_PROVIDERS:
            return _tm._redirect_invalid_session()
        subject = _tm._oauth_subject(provider)
        if not subject:
            flash("Sign-in did not complete. Please try again.", "warning")
            return redirect(url_for("client_login"))

        now = datetime.now(timezone.utc)
        account = (
            _tm.ClientAccount.query
            .filter_by(provider=provider, provider_subject=subject)
            .first()
        )
        if account is None:
            account = _tm.ClientAccount(
                id=str(uuid.uuid4()), provider=provider, provider_subject=subject,
                created_at=now, last_login_at=now,
            )
            _tm.db.session.add(account)
            _tm.log_event("client_registered", user_id=account.id, provider=provider)
        else:
            account.last_login_at = now
        _tm.db.session.commit()

        # Regenerate the session on login to defeat session fixation: preserve
        # only the post-login redirect, then establish the identity. The account
        # id becomes the client's stable user_id, so their messages link across
        # devices and "my sessions" can find their sessions.
        nxt = _tm._safe_next(session.pop("client_login_next", None))
        session.clear()
        session["user_id"]           = account.id
        session["client_account_id"] = account.id
        session.permanent = True
        _tm.log_event("client_login", user_id=account.id, provider=provider)
        return redirect(nxt or url_for("my_sessions"))
