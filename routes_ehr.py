"""
routes_ehr.py
-------------
The two HTTP endpoints for a SMART on FHIR launch out of an EHR.

  GET /ehr/launch     the EHR sends the clinician here, with iss + launch
  GET /ehr/callback   the EHR sends them back here, with code + state

The rules live in ehr.py (Flask-free, directly testable). This module owns only
the request handling: session state, redirects, and the one page the spike
renders.

PHASE 1 — nothing is stored. The access token and PKCE verifier live in the Flask
session for the length of the launch and are dropped afterwards. No patient
identifier touches the database. That is deliberate: whether patient identity
belongs in this app is a real decision, and it should be made on purpose rather
than arrived at because a spike wrote a row.

Both routes 404 when EHR_ENABLED is off, the same way the admin console hides
itself, so this is invisible in production until it is switched on.
"""

import logging

from flask import (session, request, redirect, render_template_string, abort,
                   url_for)

import config
import ehr
import TogetherMindsAI as _tm

log = logging.getLogger(__name__)

# Session keys. Namespaced so nothing else in the app collides with them.
_STATE = "_ehr_state"
_VERIFIER = "_ehr_verifier"
_ISS = "_ehr_iss"
_TOKEN_URL = "_ehr_token_url"


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


def _http_get_json(url, headers=None):
    """One GET returning parsed JSON. Raises on a non-2xx."""
    import requests
    resp = requests.get(url, headers=headers or {}, timeout=ehr.TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def _http_post_form(url, data, headers=None):
    """One form POST returning parsed JSON. Raises on a non-2xx."""
    import requests
    resp = requests.post(url, data=data, headers=headers or {},
                         timeout=ehr.TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


# The spike's only screen. A template string rather than a file: it exists to
# prove the flow returned real data, and it is deleted the moment phase 2 gives
# this a real destination.
_RESULT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>EHR launch — TogetherMindsAI</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem;max-width:40rem}
 dt{font-weight:600;margin-top:.75rem} dd{margin:0}
 .muted{color:#666} code{background:#f4f4f4;padding:.1rem .3rem;border-radius:3px}
</style></head><body>
<h1>Launch worked</h1>
<p class="muted">{{ vendor_label }} &middot; read live from the EHR. Nothing was saved.</p>
<h2>Patient</h2>
<dl>
 <dt>Name</dt><dd>{{ patient.name or "not given" }}</dd>
 <dt>Date of birth</dt><dd>{{ patient.birth_date or "not given" }}</dd>
 <dt>Gender</dt><dd>{{ patient.gender or "not given" }}</dd>
 <dt>FHIR id</dt><dd><code>{{ patient.id or "-" }}</code></dd>
</dl>
{% if encounter and encounter.id %}
<h2>Encounter</h2>
<dl>
 <dt>Status</dt><dd>{{ encounter.status or "not given" }}</dd>
 <dt>Started</dt><dd>{{ encounter.start or "not given" }}</dd>
 <dt>FHIR id</dt><dd><code>{{ encounter.id }}</code></dd>
</dl>
{% else %}
<h2>Encounter</h2>
<p class="muted">The launch carried no encounter.</p>
{% endif %}
<h2>Granted</h2>
<p class="muted"><code>{{ scope or "not reported" }}</code></p>
</body></html>
"""


def register_ehr_routes(app):
    """Attach the EHR routes to `app`. Called once at app import."""

    @app.route("/ehr/launch")
    def ehr_launch():
        """Step 1-3: check the issuer, discover its endpoints, send them on.

        The whole security posture of this route is the allowlist. `iss` arrives
        in a query string from whoever opened the link, and we then fetch
        configuration from it and later post an authorization code to it. Without
        the allowlist, a crafted launch URL pointing at a hostile server would
        hand over our client id and a live code.
        """
        _require_enabled()
        iss = request.args.get("iss", "")
        launch = request.args.get("launch", "")

        if not ehr.issuer_allowed(iss, config.EHR_ALLOWED_ISS):
            # Warning, not info: info does not reach Cloud Run's logs, and this is
            # either a misconfigured customer or someone probing.
            _tm.app.logger.warning("EHR launch refused for issuer %r", iss[:200])
            abort(400)

        if not config.EPIC_CLIENT_ID:
            _tm.app.logger.warning("EHR launch with no client id configured")
            abort(503)

        try:
            doc = _http_get_json(ehr.smart_config_url(iss))
            endpoints = ehr.endpoints_from_config(doc)
        except Exception as exc:
            _tm.app.logger.warning("EHR discovery failed for %s: %s",
                                   iss[:120], type(exc).__name__)
            abort(502)

        verifier = ehr.new_verifier()
        state = ehr.new_state()
        session[_STATE] = state
        session[_VERIFIER] = verifier
        session[_ISS] = ehr.normalise_iss(iss)
        session[_TOKEN_URL] = endpoints["token"]

        _tm.log_event("ehr_launch_started", vendor=ehr.vendor_for_iss(iss),
                      has_launch=bool(launch))

        return redirect(ehr.authorize_url(
            authorize_endpoint=endpoints["authorize"],
            client_id=config.EPIC_CLIENT_ID,
            redirect_uri=_redirect_uri(),
            scope=config.EHR_SCOPES,
            state=state,
            iss=iss,
            launch=launch,
            code_challenge=ehr.challenge_for(verifier),
        ), code=302)

    @app.route("/ehr/callback")
    def ehr_callback():
        """Step 4-5: swap the code for a token, then read the patient."""
        _require_enabled()

        # The EHR can refuse instead of returning a code — a clinician without
        # rights, or a cancelled prompt. Say so rather than 500.
        if request.args.get("error"):
            _tm.app.logger.warning("EHR callback returned error=%s",
                                   request.args.get("error")[:120])
            abort(400)

        code = request.args.get("code", "")
        state = request.args.get("state", "")
        expected = session.pop(_STATE, None)
        verifier = session.pop(_VERIFIER, None)
        iss = session.pop(_ISS, None)
        token_url = session.pop(_TOKEN_URL, None)

        # Compared with compare_digest, and only after both are known to exist.
        # A callback whose state does not match a launch WE started is either a
        # stale tab or a forged request, and either way there is nothing to do
        # with it.
        import hmac
        if not (code and state and expected and verifier and iss and token_url):
            abort(400)
        if not hmac.compare_digest(str(state), str(expected)):
            _tm.app.logger.warning("EHR callback state did not match")
            abort(400)

        headers = {"Accept": "application/json"}
        auth = ehr.basic_auth_header(config.EPIC_CLIENT_ID,
                                     config.EPIC_SANDBOX_CLIENT_SECRET)
        if auth:
            headers["Authorization"] = auth

        try:
            payload = _http_post_form(
                token_url,
                ehr.token_request_body(
                    code=code, redirect_uri=_redirect_uri(),
                    client_id=config.EPIC_CLIENT_ID, code_verifier=verifier),
                headers=headers)
            ctx = ehr.context_from_token(payload)
        except Exception as exc:
            _tm.app.logger.warning("EHR token exchange failed: %s",
                                   type(exc).__name__)
            abort(502)

        read_headers = {"Authorization": "Bearer " + ctx["access_token"],
                        "Accept": "application/fhir+json"}
        patient = {"id": None, "name": None, "birth_date": None, "gender": None}
        encounter = {"id": None, "status": None, "start": None}
        try:
            if ctx["patient"]:
                patient = ehr.patient_summary(_http_get_json(
                    ehr.resource_url(iss, "Patient", ctx["patient"]),
                    headers=read_headers))
            if ctx["encounter"]:
                encounter = ehr.encounter_summary(_http_get_json(
                    ehr.resource_url(iss, "Encounter", ctx["encounter"]),
                    headers=read_headers))
        except Exception as exc:
            _tm.app.logger.warning("EHR resource read failed: %s",
                                   type(exc).__name__)
            abort(502)

        # Metadata only. No name, no date of birth, no FHIR id — the audit log
        # takes no PII, and that rule does not bend for a new integration.
        _tm.log_event("ehr_launch_completed", vendor=ehr.vendor_for_iss(iss),
                      had_patient=bool(ctx["patient"]),
                      had_encounter=bool(ctx["encounter"]))

        vendor = ehr.vendor_for_iss(iss)
        return render_template_string(
            _RESULT_PAGE,
            vendor_label=(ehr.VENDORS.get(vendor) or {}).get("label") or "EHR",
            patient=patient, encounter=encounter, scope=ctx["scope"])
