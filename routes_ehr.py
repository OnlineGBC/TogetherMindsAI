"""
routes_ehr.py
-------------
The two HTTP endpoints for a SMART on FHIR launch out of an EHR.

  GET /ehr/launch     the EHR sends the clinician here, with iss + launch
  GET /ehr/callback   the EHR sends them back here, with code + state

This module owns ONLY what HTTP owns: reading a request, keeping launch state in
the session, turning an ehr.EhrError into a status code, and rendering. The flow
itself — discover, redirect, exchange, read — lives in ehr.py, so it can be
tested by calling a function and a second vendor does not put a second copy of
the sequence inside another view.

PHASE 1 — nothing is stored. The token and PKCE verifier live in the Flask
session for the length of the launch and are dropped afterwards. No patient
identifier reaches the database.

Both routes 404 when EHR_ENABLED is off, the same way the admin console hides
itself, so this is invisible in production until it is switched on.
"""

import logging

from flask import (session, request, redirect, render_template, abort, url_for)

import config
import ehr
import TogetherMindsAI as _tm

log = logging.getLogger(__name__)

# Session keys, namespaced so nothing else in the app collides with them.
_STATE = "_ehr_state"
_VERIFIER = "_ehr_verifier"
_ISS = "_ehr_iss"
_TOKEN_URL = "_ehr_token_url"

# The one place an ehr error becomes an HTTP status. Keeping the mapping here is
# what lets ehr.py raise meaning instead of status codes.
_STATUS = {
    ehr.EhrRefused: 400,
    ehr.EhrNotConfigured: 503,
    ehr.EhrUnavailable: 502,
}


def _status_for(exc) -> int:
    for kind, code in _STATUS.items():
        if isinstance(exc, kind):
            return code
    return 500


def _require_enabled():
    """404 unless the integration is switched on. Not 403: a route nobody is
    meant to know about should not confirm it exists."""
    if not config.EHR_ENABLED:
        abort(404)


def _redirect_uri() -> str:
    """The callback, absolute and https.

    Built with _external so it matches what was registered with the EHR exactly —
    the authorization server compares this string, and a mismatch is refused with
    an error that does not say why.
    """
    return url_for("ehr_callback", _external=True, _scheme="https")


def _tenant_lookup():
    """The tenant seam, wired to configuration for now.

    One customer today, so this reads a config tuple. When there are many, this
    is the only function that changes — it becomes a database lookup returning
    that customer's issuer, client id and authentication. The flow in ehr.py
    takes it as an argument and does not care which it is.
    """
    return ehr.tenant_from_config(
        allowed_iss=config.EHR_ALLOWED_ISS,
        client_id=config.EPIC_CLIENT_ID,
        auth=ehr.secret_auth(config.EPIC_CLIENT_ID,
                             config.EPIC_SANDBOX_CLIENT_SECRET),
    )


# --- transports. The only code here that touches the network. ---------------

def _fetch_json(url, headers=None):
    import requests
    resp = requests.get(url, headers=headers or {}, timeout=ehr.TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def _post_form(url, data, headers=None):
    import requests
    resp = requests.post(url, data=data, headers=headers or {},
                         timeout=ehr.TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def _post_json(url, body, headers=None):
    """Not used in phase 1. Passed to the flow so writing a note in phase 2 needs
    no change here."""
    import requests
    resp = requests.post(url, json=body, headers=headers or {},
                         timeout=ehr.TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def register_ehr_routes(app):
    """Attach the EHR routes to `app`. Called once at app import."""

    @app.route("/ehr/launch")
    def ehr_launch():
        _require_enabled()
        iss = request.args.get("iss", "")
        launch = request.args.get("launch", "")

        try:
            started = ehr.start_launch(
                iss=iss, launch=launch, redirect_uri=_redirect_uri(),
                scope=config.EHR_SCOPES, tenant_for=_tenant_lookup(),
                fetch_json=_fetch_json)
        except ehr.EhrError as exc:
            # Warning, not info: info does not reach Cloud Run's logs, and this is
            # either a misconfigured customer or someone probing.
            _tm.app.logger.warning("EHR launch stopped (%s) for iss=%r: %s",
                                   type(exc).__name__, iss[:200], exc)
            abort(_status_for(exc))

        session[_STATE] = started["state"]
        session[_VERIFIER] = started["verifier"]
        session[_ISS] = started["iss"]
        session[_TOKEN_URL] = started["token_url"]

        _tm.log_event("ehr_launch_started", vendor=started["vendor"],
                      has_launch=bool(launch))
        return redirect(started["redirect_to"], code=302)

    @app.route("/ehr/callback")
    def ehr_callback():
        _require_enabled()

        # The EHR can refuse instead of returning a code — a clinician without
        # rights, or a cancelled prompt. Say which, or a failed launch in
        # production is a mystery.
        if request.args.get("error"):
            _tm.app.logger.warning("EHR callback returned error=%s",
                                   (request.args.get("error") or "")[:120])
            abort(400)

        # Popped, not read: single use, so a replayed callback finds nothing.
        expected = session.pop(_STATE, None)
        verifier = session.pop(_VERIFIER, None)
        iss = session.pop(_ISS, None)
        token_url = session.pop(_TOKEN_URL, None)

        try:
            done = ehr.finish_launch(
                code=request.args.get("code", ""),
                state=request.args.get("state", ""),
                expected_state=expected, verifier=verifier, iss=iss,
                token_url=token_url, redirect_uri=_redirect_uri(),
                tenant_for=_tenant_lookup(), fetch_json=_fetch_json,
                post_form=_post_form, post_json=_post_json)
        except ehr.EhrError as exc:
            _tm.app.logger.warning("EHR callback stopped (%s): %s",
                                   type(exc).__name__, exc)
            abort(_status_for(exc))

        # Metadata only. No name, no date of birth, no FHIR id — the audit log
        # takes no PII, and that rule does not bend for a new integration.
        _tm.log_event("ehr_launch_completed", vendor=done["vendor"],
                      had_patient=bool(done["patient"]["id"]),
                      had_encounter=bool(done["encounter"]["id"]))

        return render_template("ehr_result.html",
                               vendor_label=ehr.vendor_label(done["vendor"]),
                               patient=done["patient"],
                               encounter=done["encounter"],
                               scope=done["scope"])
