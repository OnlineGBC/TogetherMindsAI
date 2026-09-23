"""
routes_ehr.py
-------------
The HTTP endpoints for a SMART on FHIR launch out of an EHR.

  GET  /ehr/launch        the EHR sends the clinician here, with iss + launch
  GET  /ehr/callback      the EHR sends them back here, with code + state
  POST /ehr/load-summary  optionally pull a TMAI session's recap into the note
  POST /ehr/write-note    the clinician files a reviewed note into the chart

This module owns ONLY what HTTP owns: reading a request, keeping launch state in
the session, turning an ehr.EhrError into a status code, and rendering. The flow
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
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from flask import (session, request, redirect, render_template, abort, url_for)
from sqlalchemy import func as sa_func

import config
import ehr
import TogetherMindsAI as _tm
from models import db, EhrLaunchContext, SessionSummary, TherapySession, friendly_name_key
from session_id import SESSION_ID_LENGTH

log = logging.getLogger(__name__)

# Session keys, namespaced so nothing else in the app collides with them.
_STATE = "_ehr_state"
_VERIFIER = "_ehr_verifier"
_ISS = "_ehr_iss"
_TOKEN_URL = "_ehr_token_url"
# The launch id is a POINTER to a server-side row, which is the only thing in
# this list safe to keep in a cookie session. Flask signs the cookie but does not
# encrypt it, so the access token itself lives in the database and never here.
_LAUNCH_ID = "_ehr_launch_id"

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


# --- the launch context row ------------------------------------------------

def _save_launch_context(done, now):
    """Keep what a later write needs, and nothing else. Returns the launch id.

    Called only when writing is switched on and the launch actually carried a
    patient — with no patient there is nothing to address a note to, so there is
    no reason to hold a token.
    """
    launch_id = str(uuid.uuid4())
    db.session.add(EhrLaunchContext(
        launch_id=launch_id,
        iss=done["iss"],
        patient_fhir_id=str(done["patient_id"]),
        encounter_fhir_id=(str(done["encounter_id"])
                           if done["encounter_id"] else None),
        fhir_user=(str(done["fhir_user"]) if done["fhir_user"] else None),
        access_token=done["access_token"],
        token_expires_at=ehr.token_expiry(done["expires_in"], now),
        created_at=now,
    ))
    db.session.commit()
    return launch_id


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


def _render_result(*, error=None, written=None, note_text="", ctx=None,
                    notice=None):
    """The one place phase 2's outcomes are rendered — a filed note, a refused
    note, or a loaded recap — so every branch reads sensibly with the patient
    block empty. 200 even on failure or a notice: the response IS what the
    clinician has to read next, not a status code."""
    return render_template(
        "ehr_result.html",
        vendor_label=ehr.vendor_label(
            ehr.vendor_for_iss(ctx.iss) if ctx else ""),
        patient={"id": None, "name": None, "birth_date": None,
                 "gender": None},
        encounter={"id": None, "status": None, "start": None},
        scope="", can_write=bool(ctx and not ctx.written_at),
        note_text=note_text, written=written, error=error, notice=notice)


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

        # Phase 2: hold the token server-side so a note can be written after the
        # session. Only when writing is on AND there is a patient to address.
        now = datetime.now(timezone.utc)
        can_write = bool(config.EHR_WRITE_ENABLED and done["patient_id"])
        session.pop(_LAUNCH_ID, None)
        if can_write:
            _sweep_expired_contexts(now)
            session[_LAUNCH_ID] = _save_launch_context(done, now)

        return render_template("ehr_result.html",
                               vendor_label=ehr.vendor_label(done["vendor"]),
                               patient=done["patient"],
                               encounter=done["encounter"],
                               scope=done["scope"],
                               can_write=can_write,
                               note_text="",
                               written=None,
                               error=None,
                               notice=None)

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

        launch_id = session.get(_LAUNCH_ID)
        ctx = (db.session.get(EhrLaunchContext, launch_id)
               if launch_id else None)
        if ctx is None:
            _tm.app.logger.warning("EHR note write had no launch context")
            return page(error="This launch is no longer open. Start again from "
                              "the patient's chart in Epic.")

        if ctx.written_at:
            # Not an error and not a second write. Saying "already filed" is the
            # whole point of keeping the row after success.
            return page(written=ctx.written_reference or "already filed", ctx=ctx,
                        error=None)

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
            return page(error="The Epic session timed out before this was "
                              "filed. Relaunch from the chart and file it "
                              "again — nothing was written.",
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
                type_display=config.EHR_NOTE_TYPE_DISPLAY)
        except ehr.EhrError as exc:
            _tm.app.logger.warning("EHR note write stopped (%s): %s",
                                   type(exc).__name__, exc)
            return page(error="Epic did not accept the note, so nothing was "
                              "filed. The text below is unchanged — you can try "
                              "again.", note_text=note_text, ctx=ctx)

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

        launch_id = session.get(_LAUNCH_ID)
        ctx = (db.session.get(EhrLaunchContext, launch_id)
               if launch_id else None)
        if ctx is None:
            return page(error="This launch is no longer open. Start again "
                              "from the patient's chart in Epic.")
        if ctx.written_at:
            return page(written=ctx.written_reference or "already filed",
                        ctx=ctx)

        raw = (request.form.get("tmai_session") or "").strip()
        if not raw:
            return page(error="Enter the TogetherMindsAI session ID or name "
                              "to load its recap.", ctx=ctx)
        if len(raw) > MAX_SESSION_LOOKUP_CHARS:
            return page(error="That is not a session ID or name.", ctx=ctx)

        ts = _find_tmai_session(raw)
        if ts is None:
            return page(error="No TogetherMindsAI session found for that ID "
                              "or name.", ctx=ctx)

        summary_row = db.session.get(SessionSummary, ts.id)
        if summary_row is None:
            return page(error="No summary is cached for that session yet. "
                              "Open its summary in TogetherMindsAI, then come "
                              "back and load it here.", ctx=ctx)

        try:
            payload = json.loads(summary_row.payload)
        except ValueError:
            return page(error="That session's summary could not be read.",
                        ctx=ctx)

        clinical = (payload.get("clinical") or "").strip()
        codes_rationale = (payload.get("codes_rationale") or "").strip()
        parts = []
        if clinical:
            parts.append(clinical)
        if codes_rationale:
            parts.append("Coding considerations:\n" + codes_rationale)
        note_text = "\n\n".join(parts)[:MAX_NOTE_CHARS]
        if not note_text:
            return page(error="That session's summary has no recap or "
                              "coding notes to load.", ctx=ctx)

        ctx.session_id = ts.id
        db.session.commit()

        # Metadata only, same rule as everywhere else in this file: which
        # session it came from, never what the recap said.
        _tm.log_event("ehr_summary_loaded", vendor=ehr.vendor_for_iss(ctx.iss),
                      chars=len(note_text))

        return page(note_text=note_text, ctx=ctx,
                    notice="Loaded from that session. Review before filing — "
                          "nothing has been sent to the chart.")
