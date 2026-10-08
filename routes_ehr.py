"""
routes_ehr.py
-------------
The HTTP endpoints for a SMART on FHIR launch out of an EHR.

  GET  /ehr/launch            the EHR sends the clinician here, with iss + launch
  GET  /ehr/callback          the EHR sends them back here, with code + state
  POST /ehr/load-summary      optionally pull a TMAI session's recap into the note
  POST /ehr/add-billing-code  optionally add a hand-picked internist E&M code
  POST /ehr/write-note        the clinician files a reviewed note into the chart

This module owns ONLY what HTTP owns: reading a request, keeping launch state in
its own cookies, turning an ehr.EhrError into a status code, and rendering. The flow
itself — discover, redirect, exchange, read, write — lives in ehr.py, so it can
be tested by calling a function and a second vendor does not put a second copy
of the sequence inside another view.

WHAT IS STORED, AND WHY IT CHANGED. Phase 1 stored nothing. Phase 2 has to,
because the note is written after the session while the token arrives before it.
What is kept is one EhrLaunchContext row: the FHIR ids, the access token, and
its expiry. FHIR IDS ONLY — no name, no date of birth, no gender. Those are read
live, rendered, and never written down, so this stays a set of pointers rather
than a patient index.

The token lives in the DATABASE, encrypted — not in the Flask session. Flask's
default session is a signed cookie, not an encrypted one, so its contents are
readable by anyone holding it. Only the launch id, a pointer we mint, goes in
the cookie.

TWO SWITCHES, NOT ONE. EHR_ENABLED makes the launch work; EHR_WRITE_ENABLED
makes the chart writable. A read going wrong shows a clinician a stale field. A
write going wrong leaves a permanent entry in a medical record. Those do not
deserve the same switch.

Every route 404s when its switch is off, the same way the admin console hides
itself, so this is invisible in production until it is switched on.

LOGIN, WHEN THE LAUNCH'S IDENTITY IS VERIFIED. If Epic granted openid/fhirUser
and the id_token checks out (see ehr.verified_identity), the callback signs the
clinician into TMAI as that practitioner — same session keys, same fixation
clear, as the Google/Microsoft flow. An unverified or absent identity signs
no one in; nothing here depends on that login succeeding.

OWN COOKIES, NOT THE FLASK SESSION. Proven live: a clinician who opened
TogetherMindsAI in a second tab (the link on the result page) and signed in
there had that tab's login clear the SHARED session — Google, Microsoft, and
this module's own EHR login all do that clear on purpose, as a fixation
defense. That wiped out the launch pointer, the PKCE state, and the
session-wide CSRF token out from under the first tab's still-open page. The
launch state (pre-callback) and the launch pointer + its own CSRF secret
(post-callback) live in their own cookies instead, untouched by a login
happening anywhere else in the same browser.
"""

import json
import logging
import secrets
import uuid
from datetime import datetime, timezone

from flask import (session, request, redirect, render_template, make_response,
                   abort, url_for)
from sqlalchemy import func as sa_func

import billing_codes
import config
import ehr
import TogetherMindsAI as _tm
from models import db, Clinician, EhrLaunchContext, SessionSummary, TherapySession, friendly_name_key
from session_id import SESSION_ID_LENGTH

log = logging.getLogger(__name__)

# Cookie names, namespaced so nothing else in the app collides with them. Own
# cookies, not Flask session keys — see the module docstring for why.
_STATE = "_ehr_state"
_VERIFIER = "_ehr_verifier"
_ISS = "_ehr_iss"
_TOKEN_URL = "_ehr_token_url"
_LAUNCH_ID = "_ehr_launch_id"

# The launch->callback round trip through Epic's own login/consent screens —
# ample, since it is a human doing that, not a redirect chain.
_LAUNCH_STATE_COOKIE_MAX_AGE = 600
# Matches the "about an hour" the result page already tells the clinician.
_LAUNCH_COOKIE_MAX_AGE = 3600


def _set_cookie(resp, name, value, max_age):
    resp.set_cookie(name, value, max_age=max_age, httponly=True,
                    secure=config.IS_PRODUCTION, samesite="Lax")

# Longest note we will accept from the form. A progress note is prose; anything
# past this is a paste accident or someone probing, and either way the chart
# should not receive it.
MAX_NOTE_CHARS = 20000

# A Session ID or friendly name is short by construction (SESSION_ID_LENGTH, or
# the friendly-name limit enforced when it was set) — well past this is not a
# real lookup.
MAX_SESSION_LOOKUP_CHARS = 200

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
    epic = ehr.tenant_from_config(
        allowed_iss=config.EHR_ALLOWED_ISS,
        client_id=config.EPIC_CLIENT_ID,
        auth=ehr.secret_auth(config.EPIC_CLIENT_ID,
                             config.EPIC_SANDBOX_CLIENT_SECRET),
    )
    cerner = ehr.tenant_from_config(
        allowed_iss=config.EHR_ALLOWED_ISS,
        client_id=config.CERNER_CLIENT_ID,
        auth=ehr.secret_auth(config.CERNER_CLIENT_ID,
                             config.CERNER_CLIENT_SECRET),
    )

    def lookup(iss):
        # Exact issuer match picks the credentials — never vendor_for_iss,
        # which is a wording heuristic. The allowlist check runs inside either.
        return (cerner if _is_cerner(iss) else epic)(iss)
    return lookup


def _is_cerner(iss):
    """True for an issuer configured as Oracle Health (Cerner). Exact match."""
    return ehr.normalise_iss(iss) in {ehr.normalise_iss(c) for c in config.CERNER_ISS}


def _provider_for(iss):
    """The Clinician.provider an EHR login is stored under. Same exact-match
    rule as the credentials, so a Cerner practitioner can never land on an
    Epic account that happens to share a subject string."""
    return "oracle" if _is_cerner(iss) else "epic"


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
    """Create a FHIR resource. Phase 2's note goes through here.

    A FHIR create answers 201 with a Location header and is ALLOWED to return an
    empty body — Epic normally does. So this cannot just call resp.json(): that
    raises on an empty body, the caller reports a failure for a note that landed,
    the clinician presses the button again, and the chart ends up with two
    progress notes. The status and the location are handed back under underscore
    keys so `ehr.created_reference` can read them without pretending they came
    from the server's JSON.
    """
    import requests
    resp = requests.post(url, json=body, headers=headers or {},
                         timeout=ehr.TIMEOUT_SECONDS)
    if not resp.ok:
        # Epic's rejection reason (an OperationOutcome) is diagnostic, not
        # clinical content — it describes what WE sent wrong (scope, shape),
        # never what the note said. Logged here because ehr.py's create()
        # only keeps the exception's type name once it re-raises.
        log.warning("EHR create refused: %s %s", resp.status_code,
                   (resp.text or "")[:2000])
    resp.raise_for_status()
    out = {}
    if (resp.content or b"").strip():
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                out = dict(parsed)
        except ValueError:
            # 201 with a body we cannot parse is still a successful create. The
            # Location header is what we actually needed.
            pass
    out.setdefault("_status", resp.status_code)
    out.setdefault("_location", resp.headers.get("Location")
                   or resp.headers.get("Content-Location") or "")
    return out


# --- logging the clinician in, when the launch's identity is trustworthy ---

def _login_via_ehr(done, now):
    """Log the clinician in AS THIS EHR PRACTITIONER (Epic or Oracle Health),
    mirroring the Google / Microsoft flow in routes_oauth.py exactly — same session keys, same
    fixation-prevention clear, same disabled-account block — so the rest of
    the app (the role gate, "my sessions", everything) treats this login no
    differently than any other.

    A no-op unless `fhir_user_verified` is True and a subject came with it.
    Nothing in the launch/read/write flow depends on this login succeeding —
    it is purely additive, so refusing here costs nothing and an unverified
    or absent identity must NEVER be trusted to sign anyone in.

    The session.clear() below is still exactly right — it is the launch
    pointer and CSRF secret living in the SESSION that used to be the actual
    bug; those are their own cookies now (see the module docstring), so this
    clear no longer takes the open launch down with it.
    """
    if not done.get("fhir_user_verified") or not done.get("epic_subject"):
        return
    subject = done["epic_subject"]
    provider = _provider_for(done.get("iss"))
    clinician = (Clinician.query
                .filter_by(provider=provider, provider_subject=subject)
                .first())
    if clinician is not None and clinician.disabled_at is not None:
        _tm.app.logger.warning(
            "DISABLED-BLOCK at EHR sign-in: id=%s subject=%s disabled_at=%s",
            clinician.id, subject[:12], clinician.disabled_at)
        _tm.log_event("clinician_login_blocked", user_id=clinician.id,
                      provider=provider)
        return
    if clinician is None:
        clinician = Clinician(id=str(uuid.uuid4()), provider=provider,
                              provider_subject=subject, created_at=now,
                              last_login_at=now)
        db.session.add(clinician)
        _tm.log_event("clinician_registered", user_id=clinician.id, provider=provider)
    else:
        clinician.last_login_at = now
    db.session.commit()

    # Same fixation-prevention clear as the Google/Microsoft flow — this is a
    # privilege change, and dropping anything pre-seeded before establishing
    # the new identity matters exactly as much here as it does there.
    session.clear()
    session["user_id"] = clinician.id
    session["clinician_id"] = clinician.id
    session.permanent = True
    _tm.log_event("clinician_login", user_id=clinician.id, provider=provider)


# --- the launch context row ------------------------------------------------

def _save_launch_context(done, now):
    """Keep what a later write needs, and nothing else. Returns the row — the
    caller needs both its id (for the launch cookie) and its own CSRF secret
    (for the form), not just the id.

    Called only when writing is switched on and the launch actually carried a
    patient — with no patient there is nothing to address a note to, so there is
    no reason to hold a token.
    """
    row = EhrLaunchContext(
        launch_id=str(uuid.uuid4()),
        iss=done["iss"],
        patient_fhir_id=str(done["patient_id"]),
        encounter_fhir_id=(str(done["encounter_id"])
                           if done["encounter_id"] else None),
        fhir_user=(str(done["fhir_user"]) if done["fhir_user"] else None),
        access_token=done["access_token"],
        token_expires_at=ehr.token_expiry(done["expires_in"], now),
        created_at=now,
        csrf_token=secrets.token_urlsafe(32),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _sweep_expired_contexts(now):
    """Drop rows whose token has died.

    An expired access token cannot be used for anything, so a row holding one is
    pure liability. Swept on the way through a launch rather than on a timer:
    there is no scheduler here, and the only moment this table grows is a launch.
    """
    try:
        (EhrLaunchContext.query
         .filter(EhrLaunchContext.token_expires_at < now)
         .delete(synchronize_session=False))
        db.session.commit()
    except Exception:
        # Housekeeping must never be the reason a launch fails.
        db.session.rollback()
        log.warning("EHR launch-context sweep failed", exc_info=True)


def _current_launch_ctx():
    """The EhrLaunchContext for this browser's open launch, from its own
    cookie — not the Flask session, which a login happening anywhere else in
    the same browser clears."""
    launch_id = request.cookies.get(_LAUNCH_ID)
    return db.session.get(EhrLaunchContext, launch_id) if launch_id else None


def _launch_csrf_ok(ctx) -> bool:
    """Whether this POST carries the token tied to THIS launch, not a
    session-wide one (see the module docstring for why that distinction
    exists). Gated by the same switch as the app's generic CSRF check, and
    for the same reason: off under the pytest runner by default, so the
    suite does not have to thread a token through every EHR POST; the
    dedicated CSRF tests flip it on same as they already do for every other
    route."""
    if not _tm._csrf_enabled():
        return True
    submitted = request.form.get("csrf_token") or ""
    expected = (ctx.csrf_token or "") if ctx else ""
    return bool(expected) and secrets.compare_digest(submitted, expected)


def _find_tmai_session(raw: str):
    """Resolve a clinician-typed Session ID, "SessionID-FriendlyName" combined
    form, or friendly name on its own, to a TherapySession. Same lookup the
    console's own rejoin form uses, so a clinician can paste the same thing
    they would paste there. Case-insensitive throughout."""
    ts = TherapySession.query.filter(
        sa_func.upper(TherapySession.id) == raw.upper()
    ).first()
    if not ts and len(raw) > SESSION_ID_LENGTH:
        ts = TherapySession.query.filter(
            sa_func.upper(TherapySession.id) == raw[:SESSION_ID_LENGTH].upper()
        ).first()
    if not ts:
        ts = TherapySession.query.filter(
            TherapySession.friendly_name_key == friendly_name_key(raw)
        ).first()
    return ts


def _vendor_label(ctx):
    """The vendor name for an open launch ("Epic", "Oracle Health"), else "EHR"."""
    return ehr.vendor_label(ehr.vendor_for_iss(ctx.iss) if ctx else "")


def _render_result(*, error=None, written=None, note_text="", ctx=None,
                    notice=None, tmai_session=None):
    """The one place phase 2's outcomes are rendered — a filed note, a refused
    note, or a loaded recap — so every branch reads sensibly with the patient
    block empty. 200 even on failure or a notice: the response IS what the
    clinician has to read next, not a status code.

    `tmai_session` re-fills the "Session ID or name" box. Once a launch is
    linked (ctx.session_id set), that's the default for every render after —
    the clinician typed a name once and it stays visible, rather than reading
    as blank and unlinked on every subsequent page."""
    if tmai_session is None:
        tmai_session = (ctx.session_id if ctx else "") or ""
    return render_template(
        "ehr_result.html",
        vendor_label=_vendor_label(ctx),
        patient={"id": None, "name": None, "birth_date": None,
                 "gender": None},
        encounter={"id": None, "status": None, "start": None},
        scope="", can_write=bool(ctx and not ctx.written_at),
        note_text=note_text, written=written, error=error, notice=notice,
        tmai_session=tmai_session, em_codes=billing_codes.INTERNIST_EM_CODES,
        ehr_csrf_token=(ctx.csrf_token if ctx else "") or "")


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

        resp = make_response(redirect(started["redirect_to"], code=302))
        for name, value in ((_STATE, started["state"]),
                           (_VERIFIER, started["verifier"]),
                           (_ISS, started["iss"]),
                           (_TOKEN_URL, started["token_url"])):
            _set_cookie(resp, name, value, _LAUNCH_STATE_COOKIE_MAX_AGE)

        _tm.log_event("ehr_launch_started", vendor=started["vendor"],
                      has_launch=bool(launch))
        return resp

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

        # Read from the cookies set at /ehr/launch — their own, short-lived
        # ones, not the Flask session (see the module docstring for why).
        expected = request.cookies.get(_STATE)
        verifier = request.cookies.get(_VERIFIER)
        iss = request.cookies.get(_ISS)
        token_url = request.cookies.get(_TOKEN_URL)

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

        now = datetime.now(timezone.utc)

        # Log the clinician in AS THIS EHR PRACTITIONER. Purely additive —
        # see _login_via_ehr's docstring for why its session.clear() no
        # longer threatens the launch pointer this sets up next.
        _login_via_ehr(done, now)

        # Phase 2: hold the token server-side so a note can be written after the
        # session. Only when writing is on AND there is a patient to address.
        can_write = bool(config.EHR_WRITE_ENABLED and done["patient_id"])
        ctx_row = None
        if can_write:
            _sweep_expired_contexts(now)
            ctx_row = _save_launch_context(done, now)

        resp = make_response(render_template(
            "ehr_result.html",
            vendor_label=ehr.vendor_label(done["vendor"]),
            patient=done["patient"],
            encounter=done["encounter"],
            scope=done["scope"],
            can_write=can_write,
            note_text="",
            written=None,
            error=None,
            notice=None,
            tmai_session="",
            em_codes=billing_codes.INTERNIST_EM_CODES,
            ehr_csrf_token=(ctx_row.csrf_token if ctx_row else "") or ""))
        # Done with the launch-state cookies either way — a second use of this
        # same callback code will fail at Epic regardless (codes are
        # single-use there), so this is cleanup, not the security boundary.
        for name in (_STATE, _VERIFIER, _ISS, _TOKEN_URL):
            resp.delete_cookie(name)
        if ctx_row:
            _set_cookie(resp, _LAUNCH_ID, ctx_row.launch_id,
                       _LAUNCH_COOKIE_MAX_AGE)
        return resp

    @app.route("/ehr/write-note", methods=["POST"])
    def ehr_write_note():
        """File the clinician's reviewed note into the chart.

        A POST from a plain form, not a socket emit, because this is a
        state-changing action against someone else's system of record and the
        simplest reliable transport is the right one.

        It CONFIRMS before it moves: every outcome renders this same page saying
        what happened. Nothing here redirects on success and nothing abandons the
        page on failure, because a clinician who is not told whether the note
        landed will press the button again.
        """
        _require_enabled()
        if not config.EHR_WRITE_ENABLED:
            # Same reasoning as the feature flag on the launch routes: a route
            # nobody is meant to know about should not confirm it exists.
            abort(404)

        page = _render_result  # 200 even on failure — see _render_result.

        ctx = _current_launch_ctx()
        if ctx is None:
            _tm.app.logger.warning("EHR note write had no launch context")
            return page(error="This launch is no longer open. Start again from "
                              "the patient's chart.")

        if ctx.written_at:
            # Not an error and not a second write. Saying "already filed" is the
            # whole point of keeping the row after success.
            return page(written=ctx.written_reference or "already filed", ctx=ctx,
                        error=None)

        if not _launch_csrf_ok(ctx):
            return page(error="This page is no longer current. Reload it "
                              "from the patient's chart and try again.", ctx=ctx)

        note_text = (request.form.get("note_text") or "").strip()
        if not note_text:
            return page(error="There is nothing written to file.", ctx=ctx)
        if len(note_text) > MAX_NOTE_CHARS:
            return page(error="That note is too long to file (limit %d "
                              "characters)." % MAX_NOTE_CHARS,
                        note_text=note_text[:MAX_NOTE_CHARS], ctx=ctx)

        now = datetime.now(timezone.utc)
        if not ehr.token_usable(ctx.token_expires_at, now):
            # No refresh token exists — "Requires Persistent Access" is off on
            # the Epic registration — so this is genuinely unrecoverable here.
            # Say so plainly instead of sending a dead token at a chart.
            _tm.app.logger.warning("EHR note write refused: token expired")
            return page(error="The %s session timed out before this was "
                              "filed. Relaunch from the chart and file it "
                              "again — nothing was written."
                              % _vendor_label(ctx),
                        note_text=note_text, ctx=ctx)

        client = ehr.FhirClient(iss=ctx.iss, token=ctx.access_token,
                                fetch_json=_fetch_json, post_json=_post_json)
        try:
            result = ehr.write_note(
                client=client, note_text=note_text,
                patient_id=ctx.patient_fhir_id,
                encounter_id=ctx.encounter_fhir_id,
                author=ctx.fhir_user, now=now,
                type_code=config.EHR_NOTE_TYPE_CODE,
                type_display=config.EHR_NOTE_TYPE_DISPLAY,
                relative_author=_is_cerner(ctx.iss),
                content_type=("text/plain; charset=utf-8" if _is_cerner(ctx.iss)
                              else "text/plain"))
        except ehr.EhrError as exc:
            _tm.app.logger.warning("EHR note write stopped (%s): %s",
                                   type(exc).__name__, exc)
            return page(error="%s did not accept the note, so nothing was "
                              "filed. The text below is unchanged — you can try "
                              "again." % _vendor_label(ctx),
                        note_text=note_text, ctx=ctx)

        ctx.written_at = now
        ctx.written_reference = result["reference"] or "filed"
        # The token has done its only job. Dropping it here rather than waiting
        # for the sweep means a written note leaves no live credential behind.
        ctx.access_token = ""
        db.session.commit()

        # Metadata only, again: whether it landed, never what it said.
        _tm.log_event("ehr_note_written",
                      vendor=ehr.vendor_for_iss(ctx.iss),
                      chars=len(note_text),
                      had_encounter=bool(ctx.encounter_fhir_id))

        return page(written=ctx.written_reference, ctx=ctx)

    @app.route("/ehr/load-summary", methods=["POST"])
    def ehr_load_summary():
        """Load a TogetherMindsAI session's cached recap into the note box, so
        the clinician is not typing the whole thing by hand.

        Loads only. Nothing here reaches Epic — it fills the textarea on this
        same page, and "File this note in the chart" is still the only button
        that writes anything, same as if the clinician had typed it themself.
        """
        _require_enabled()
        if not config.EHR_WRITE_ENABLED:
            abort(404)

        page = _render_result

        ctx = _current_launch_ctx()
        if ctx is None:
            return page(error="This launch is no longer open. Start again "
                              "from the patient's chart.")
        if ctx.written_at:
            return page(written=ctx.written_reference or "already filed",
                        ctx=ctx)
        if not _launch_csrf_ok(ctx):
            return page(error="This page is no longer current. Reload it "
                              "from the patient's chart and try again.", ctx=ctx)

        raw = (request.form.get("tmai_session") or "").strip()
        if not raw:
            return page(error="Enter the TogetherMindsAI session ID or name "
                              "to load its recap.", ctx=ctx)
        if len(raw) > MAX_SESSION_LOOKUP_CHARS:
            return page(error="That is not a session ID or name.", ctx=ctx,
                        tmai_session=raw)

        ts = _find_tmai_session(raw)
        if ts is None:
            return page(error="No TogetherMindsAI session found for that ID "
                              "or name.", ctx=ctx, tmai_session=raw)

        summary_row = db.session.get(SessionSummary, ts.id)
        if summary_row is None:
            return page(error="No summary is cached for that session yet. "
                              "Open its summary in TogetherMindsAI, then come "
                              "back and load it here.", ctx=ctx, tmai_session=raw)

        try:
            payload = json.loads(summary_row.payload)
        except ValueError:
            return page(error="That session's summary could not be read.",
                        ctx=ctx, tmai_session=raw)

        clinical = (payload.get("clinical") or "").strip()
        codes_rationale = (payload.get("codes_rationale") or "").strip()
        parts = []
        if clinical:
            parts.append(clinical)
        if codes_rationale:
            parts.append("Coding considerations:\n" + codes_rationale)

        billing_lines = []
        cpt = payload.get("cpt_suggestion")
        if cpt:
            billing_lines.append("CPT %s — %s" % (cpt["code"], cpt["label"]))
        pos = payload.get("pos")
        if pos:
            billing_lines.append("Place of Service %s — %s" % (pos["code"], pos["label"]))
        if billing_lines:
            parts.append("Billing code considerations:\n" + "\n".join(billing_lines))

        note_text = "\n\n".join(parts)[:MAX_NOTE_CHARS]
        if not note_text:
            return page(error="That session's summary has no recap or "
                              "coding notes to load.", ctx=ctx, tmai_session=raw)

        ctx.session_id = ts.id
        db.session.commit()

        # Metadata only, same rule as everywhere else in this file: which
        # session it came from, never what the recap said.
        _tm.log_event("ehr_summary_loaded", vendor=ehr.vendor_for_iss(ctx.iss),
                      chars=len(note_text))

        return page(note_text=note_text, ctx=ctx,
                    notice="Loaded from that session. Review before filing — "
                          "nothing has been sent to the chart.")

    @app.route("/ehr/add-billing-code", methods=["POST"])
    def ehr_add_billing_code():
        """Add a hand-picked internist E&M code to the note box.

        TMAI has no chief complaint, exam findings, or medical decision-making
        to go on, so it has no basis to choose one of these itself (see
        billing_codes.py) — this is the clinician's own pick from a fixed
        list, added the same way "Load recap" adds text. Nothing here reaches
        Epic; only "File this note" does.
        """
        _require_enabled()
        if not config.EHR_WRITE_ENABLED:
            abort(404)

        page = _render_result

        ctx = _current_launch_ctx()
        if ctx is None:
            return page(error="This launch is no longer open. Start again "
                              "from the patient's chart.")
        if ctx.written_at:
            return page(written=ctx.written_reference or "already filed",
                        ctx=ctx)
        if not _launch_csrf_ok(ctx):
            return page(error="This page is no longer current. Reload it "
                              "from the patient's chart and try again.", ctx=ctx)

        current = (request.form.get("note_text") or "").rstrip()
        picked = billing_codes.internist_em_code(
            (request.form.get("em_code") or "").strip())
        if picked is None:
            return page(error="Pick a billing code from the list.",
                        note_text=current, ctx=ctx)

        addition = "Billing code considerations:\nCPT %s — %s" % (
            picked["code"], picked["label"])
        note_text = ((current + "\n\n" + addition) if current else addition
                    )[:MAX_NOTE_CHARS]

        return page(note_text=note_text, ctx=ctx,
                    notice="Added that code to the note. Review before "
                          "filing — nothing has been sent to the chart.")
