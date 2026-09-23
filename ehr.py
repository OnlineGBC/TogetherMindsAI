"""
ehr.py
------
SMART on FHIR — launching TogetherMindsAI from inside an EHR (Epic, Oracle Health).

Flask-free and network-free on purpose, like billing.py and admin_access.py. Every
rule and the whole FLOW live here and are testable by calling a function; the HTTP
layer in routes_ehr.py only gathers a request, calls in, and renders.

The flow, once, so the pieces below make sense:

  1. The EHR opens our launch URL with `iss` (its FHIR base) and `launch` (an
     opaque handle for "the patient currently open").
  2. We ask that base for its SMART configuration to learn where to send the
     clinician and where to exchange the code.
  3. We redirect them to the EHR's authorize endpoint. They are already signed
     into the EHR, so they are usually bounced straight back.
  4. The EHR calls our callback with a code. We exchange it server-side for an
     access token, and the token response also names the patient.
  5. We read that patient with the token.

THREE SEAMS, because this is going to grow. Each is a place to extend rather than
a place to reopen:

  * `tenant_for` is INJECTED into the flow. Today it reads a config tuple and
    returns one tenant. When there are fifty customers it reads a database table.
    The flow does not change either way — which is the whole point, because each
    health system arrives with its own FHIR base URL and its own client
    activation.
  * AUTHENTICATION is a function, not a secret string. `secret_auth` is the
    sandbox. Epic's own server advertises `private_key_jwt` for production
    (see token_endpoint_auth_methods_supported), so that becomes a second
    function, not a rewrite.
  * `FhirClient` carries the base, the token and the transport. Reads go through
    it now; writing a note in phase 2 is a method on it, and nothing else moves.

Two guards are load-bearing rather than decoration:

  * `iss` arrives in a query string and is then TRUSTED — we fetch configuration
    from it and post an authorization code to it. An attacker who can choose it
    can harvest both. The tenant lookup is the gate, and nothing here talks to a
    base that has not passed it.
  * PKCE. The authorization code travels through the clinician's browser. Without
    a verifier, anyone who intercepts it can redeem it.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone

import jwt

log = logging.getLogger(__name__)

# How long to wait on an EHR. Short: a clinician is staring at a blank tab, and a
# hung discovery call is worse than a clear failure.
TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Errors.
#
# These carry MEANING, not HTTP status codes. The routes decide what a refusal
# looks like over HTTP; if status codes appeared here, this module would be back
# to knowing about Flask and the split would be for nothing.
# ---------------------------------------------------------------------------

class EhrError(Exception):
    """Anything that stopped a launch."""


class EhrRefused(EhrError):
    """We will not proceed: unknown issuer, bad state, no code. Our decision."""


class EhrNotConfigured(EhrError):
    """We would proceed but we are not set up to — no client id, no tenant."""


class EhrUnavailable(EhrError):
    """The EHR did not hold up its end: discovery down, token exchange refused,
    a resource read that failed."""


# ---------------------------------------------------------------------------
# Who we are willing to talk to
# ---------------------------------------------------------------------------

def normalise_iss(iss: str) -> str:
    """A FHIR base URL in the one form we compare. Trailing slashes only."""
    return (iss or "").strip().rstrip("/")


def issuer_allowed(iss: str, allowed) -> bool:
    """True when this issuer is one we are configured to trust.

    An exact match after normalising, deliberately — NOT a prefix or "endswith"
    test. `https://attacker.example/?u=<allowed>` ends with the allowed base, and
    `<allowed>.attacker.example` starts with it, so each of the careless versions
    lets one of them through. Exact is the only version with no way around it.

    https is required. The token exchange carries a credential.
    """
    base = normalise_iss(iss)
    if not base or not base.lower().startswith("https://"):
        return False
    return base in {normalise_iss(a) for a in (allowed or ())}


def tenant_from_config(*, allowed_iss, client_id, auth):
    """The simplest `tenant_for`: one set of credentials, a fixed list of bases.

    This is the seam. It has the same shape a database-backed lookup will have —
    take an issuer, return that customer's credentials or raise — so replacing it
    later is a swap and not a redesign.
    """
    def lookup(iss: str) -> dict:
        if not issuer_allowed(iss, allowed_iss):
            raise EhrRefused("issuer is not one we are configured to trust")
        if not client_id:
            raise EhrNotConfigured("no client id configured for this issuer")
        return {"iss": normalise_iss(iss), "client_id": client_id, "auth": auth}
    return lookup


# ---------------------------------------------------------------------------
# How we prove who we are at the token endpoint.
#
# A function rather than a secret string, so production's asymmetric method is a
# sibling of this one instead of a change to the flow. Each returns the headers
# and the extra body fields the exchange needs.
# ---------------------------------------------------------------------------

def secret_auth(client_id: str, client_secret: str):
    """HTTP Basic, which Epic advertises as client_secret_basic.

    The secret goes in a HEADER and never in the form body: bodies end up in more
    logs, proxies and error reports than headers do.
    """
    def apply() -> dict:
        if not client_secret:
            # A public client identifies itself in the body and nothing else.
            return {"headers": {}, "body": {"client_id": client_id}}
        raw = f"{client_id}:{client_secret}".encode()
        token = base64.b64encode(raw).decode("ascii")
        return {"headers": {"Authorization": "Basic " + token},
                "body": {"client_id": client_id}}
    return apply


def public_auth(client_id: str):
    """No credential at all. Only correct for a client that cannot hold one."""
    return secret_auth(client_id, "")


# NOTE for production: Epic's SMART configuration lists
# token_endpoint_auth_methods_supported = [client_secret_post,
# client_secret_basic, private_key_jwt]. The third is what a production
# confidential client uses, and it belongs here as `private_key_jwt_auth`,
# returning {"headers": {}, "body": {"client_assertion": …,
# "client_assertion_type": …}}. Deliberately not written yet: it needs a keypair
# and a JWKS hosted on our domain, and there is no production client to use it.


# ---------------------------------------------------------------------------
# PKCE and state
# ---------------------------------------------------------------------------

def new_verifier() -> str:
    """A fresh PKCE code verifier. 43-128 chars of unreserved characters per
    RFC 7636; token_urlsafe(64) lands inside that and is uniformly random."""
    return secrets.token_urlsafe(64)


def challenge_for(verifier: str) -> str:
    """The S256 challenge for a verifier — base64url of its SHA-256, no padding.

    Padding is stripped because RFC 7636 says so, and Epic rejects a challenge
    that carries '='.
    """
    digest = hashlib.sha256((verifier or "").encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_state() -> str:
    """An opaque value echoed back on the callback, so a callback that did not
    come from a launch we started can be refused."""
    return secrets.token_urlsafe(32)


def state_matches(got, expected) -> bool:
    """Constant-time comparison, and False unless BOTH sides exist — an absent
    expectation must never compare equal to an absent answer."""
    if not got or not expected:
        return False
    return hmac.compare_digest(str(got), str(expected))


# ---------------------------------------------------------------------------
# SMART discovery
# ---------------------------------------------------------------------------

def smart_config_url(iss: str) -> str:
    return normalise_iss(iss) + "/.well-known/smart-configuration"


def endpoints_from_config(doc) -> dict:
    """{authorize, token} from a SMART configuration document.

    Raises when either is missing. A launch cannot proceed without both, and
    guessing conventional paths would mean sending a clinician — and an
    authorization code — somewhere the server never advertised.
    """
    if not isinstance(doc, dict):
        raise EhrUnavailable("SMART configuration was not a JSON object.")
    authorize = (doc.get("authorization_endpoint") or "").strip()
    token = (doc.get("token_endpoint") or "").strip()
    missing = [name for name, value in
               (("authorization_endpoint", authorize), ("token_endpoint", token))
               if not value]
    if missing:
        raise EhrUnavailable("SMART configuration is missing: " + ", ".join(missing))
    return {"authorize": authorize, "token": token}


# ---------------------------------------------------------------------------
# The authorize redirect
# ---------------------------------------------------------------------------

def authorize_url(*, authorize_endpoint, client_id, redirect_uri, scope, state,
                  iss, launch, code_challenge) -> str:
    """The URL to redirect the clinician to.

    `aud` is the FHIR base, and it is required rather than nice to have: it tells
    the authorization server which resource server the token is for, so a token
    minted for one EHR cannot be replayed against another.

    `launch` is passed straight back — it is the EHR's own handle for the patient
    on screen, opaque to us.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "aud": normalise_iss(iss),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if launch:
        params["launch"] = launch
    joiner = "&" if "?" in authorize_endpoint else "?"
    return authorize_endpoint + joiner + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# The token exchange
# ---------------------------------------------------------------------------

def token_request_body(*, code, redirect_uri, code_verifier) -> dict:
    """Form fields for the code-for-token exchange, before authentication adds
    its own.

    `redirect_uri` is repeated here even though the code came back to it. The
    spec requires it, and the server compares the two — which is what stops a
    code issued for our app being redeemed against a different redirect.
    """
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }


def context_from_token(payload) -> dict:
    """What the token response tells us, in the shape the rest of the code wants.

    The patient id arrives as `patient` — a context field, not a scope — and its
    absence is the normal case for a launch with no patient open, so it is
    reported as None rather than raised.
    """
    if not isinstance(payload, dict):
        raise EhrUnavailable("Token response was not a JSON object.")
    token = (payload.get("access_token") or "").strip()
    if not token:
        raise EhrUnavailable("Token response carried no access_token.")
    return {
        "access_token": token,
        "token_type": payload.get("token_type") or "Bearer",
        "expires_in": payload.get("expires_in"),
        "patient": payload.get("patient") or None,
        "encounter": payload.get("encounter") or None,
        "scope": payload.get("scope") or "",
        "fhir_user": payload.get("fhirUser") or payload.get("fhir_user") or None,
        "id_token": payload.get("id_token") or None,
    }


# ---------------------------------------------------------------------------
# Verifying WHO the EHR says is logged in.
#
# `fhir_user` above is read straight out of the token response with no check —
# fine for today's only use (labelling a note's author back to Epic, which Epic
# itself can cross-check). It stops being fine the moment anything inside TMAI
# — a login, in particular — decides who someone is FROM this value, because
# then a token meant for a different application, or a tampered one, would be
# indistinguishable from a real one. This is that check: signature, issuer, and
# audience, against Epic's own published keys, not a value we invented.
# ---------------------------------------------------------------------------

def openid_config_url(iss: str) -> str:
    return normalise_iss(iss) + "/.well-known/openid-configuration"


def jwks_url_from_config(doc) -> str:
    if not isinstance(doc, dict):
        raise EhrUnavailable("OpenID configuration was not a JSON object.")
    url = (doc.get("jwks_uri") or "").strip()
    if not url:
        raise EhrUnavailable("OpenID configuration is missing jwks_uri.")
    return url


def select_signing_key(jwks_doc, id_token: str) -> dict:
    """The JWK matching this token's `kid`, out of an already-fetched key set.

    Epic rotates its signing keys, so the token itself names which one signed
    it rather than there being a single key to assume.
    """
    if not isinstance(jwks_doc, dict) or not isinstance(jwks_doc.get("keys"), list):
        raise EhrRefused("JWKS document was not in the expected shape.")
    try:
        kid = jwt.get_unverified_header(id_token).get("kid")
    except Exception as exc:
        raise EhrRefused("identity token header could not be read") from exc
    for key in jwks_doc["keys"]:
        if key.get("kid") == kid:
            return key
    raise EhrRefused("no signing key matched this identity token's kid")


def verify_id_token(*, id_token, jwk, issuer, audience) -> dict:
    """Verify Epic's signed identity assertion and return its claims.

    The signature alone only proves EPIC signed something. `issuer` and
    `audience` are what prove it was signed for THIS launch and THIS app —
    without them, a genuine token issued to a different application could be
    replayed against ours. Any failure here is a refusal, not a best guess.
    """
    try:
        key = jwt.PyJWK.from_dict(jwk).key
        claims = jwt.decode(
            id_token, key=key, algorithms=[jwk.get("alg") or "RS256"],
            audience=audience, issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud"]})
    except jwt.PyJWTError as exc:
        raise EhrRefused("identity token did not verify: %s" % exc) from exc
    return claims


def verified_identity(*, id_token, iss, audience, fallback_fhir_user, fetch_json):
    """The clinician identity Epic asserted, verified when possible.

    Returns (fhir_user, verified). NEVER RAISES: missing discovery, a missing
    id_token, or a failed check must degrade this launch to "unverified", not
    break an otherwise-successful one — nothing today depends on verification
    succeeding. `verified=True` is the only value anything security-sensitive
    (a future login) may act on. `fhir_user` still falls back to the
    unverified value for today's low-stakes use (labelling a note's author),
    matching what the launch already did before this existed.
    """
    if not id_token:
        return fallback_fhir_user, False
    try:
        config_doc = fetch_json(openid_config_url(iss), headers=None)
        jwks_doc = fetch_json(jwks_url_from_config(config_doc), headers=None)
        jwk = select_signing_key(jwks_doc, id_token)
        claims = verify_id_token(id_token=id_token, jwk=jwk, issuer=iss, audience=audience)
    except Exception as exc:
        log.warning("EHR identity token did not verify (%s): %s",
                    type(exc).__name__, exc)
        return fallback_fhir_user, False
    return (claims.get("fhirUser") or fallback_fhir_user), True


# ---------------------------------------------------------------------------
# How long the token is good for.
#
# Phase 2 writes a note AFTER a session, which is minutes to an hour after the
# token arrived. So the expiry stops being a detail and becomes a thing we have
# to hold. "Requires Persistent Access" is off on our Epic registration, so
# there is no refresh token — when this runs out the write is simply gone, and
# saying so is better than sending a dead bearer token at a chart.
# ---------------------------------------------------------------------------

# Used when the token response did not say how long it has. Short deliberately:
# assuming a LONG life is the dangerous direction — it produces a write attempt
# with a token the server already dropped, and the clinician sees a failure they
# cannot act on. A short guess just asks them to launch again.
FALLBACK_TOKEN_SECONDS = 300

# Do not attempt a write inside this much of the expiry. A chart write is one
# round trip plus Epic's own processing, and a token that dies mid-write is the
# worst case: we cannot tell a refusal from a note that landed.
EXPIRY_MARGIN_SECONDS = 60


def token_expiry(expires_in, now) -> datetime:
    """When the access token dies, as an absolute UTC time.

    Absolute, not a duration, because it gets stored and read back later by a
    different request — a duration would silently mean "from whenever you happen
    to read this".
    """
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = FALLBACK_TOKEN_SECONDS
    if seconds <= 0:
        seconds = FALLBACK_TOKEN_SECONDS
    return now + timedelta(seconds=seconds)


def token_usable(expires_at, now, margin_seconds=EXPIRY_MARGIN_SECONDS) -> bool:
    """True when there is enough of the token left to attempt a write.

    A missing expiry counts as NOT usable. An unknown expiry on a credential we
    are about to write a medical record with is not a thing to be optimistic
    about.
    """
    if not expires_at:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at - timedelta(seconds=margin_seconds) > now


# ---------------------------------------------------------------------------
# Talking to the FHIR server.
#
# One object holding the base, the token and the transport. The transport is
# injected, so this module still reaches no network and the whole flow can be
# tested by passing plain functions.
#
# Phase 2 writes a session summary back to the chart. That is `create` below —
# a method here, not a change anywhere else.
# ---------------------------------------------------------------------------

class FhirClient:

    def __init__(self, *, iss, token, fetch_json, post_json=None):
        self.iss = normalise_iss(iss)
        self.token = token
        self._fetch_json = fetch_json
        self._post_json = post_json

    def _headers(self, content=False) -> dict:
        out = {"Authorization": "Bearer " + str(self.token),
               "Accept": "application/fhir+json"}
        if content:
            out["Content-Type"] = "application/fhir+json"
        return out

    def url(self, kind: str, resource_id=None) -> str:
        """The URL for a resource type, or one resource.

        The id is percent-encoded: it comes from the EHR, and an id containing a
        slash or a dot-dot would otherwise change which endpoint we call.
        """
        base = "%s/%s" % (self.iss, kind)
        if resource_id is None:
            return base
        return base + "/" + urllib.parse.quote(str(resource_id), safe="")

    def read(self, kind: str, resource_id):
        """One resource. Raises EhrUnavailable when the server will not give it."""
        try:
            return self._fetch_json(self.url(kind, resource_id),
                                    headers=self._headers())
        except Exception as exc:
            raise EhrUnavailable("could not read %s: %s"
                                 % (kind, type(exc).__name__)) from exc

    def create(self, kind: str, body):
        """Write a new resource — phase 2's DocumentReference lands here.

        Refuses rather than pretends when no write transport was supplied, so a
        read-only client cannot silently drop a write.
        """
        if self._post_json is None:
            raise EhrNotConfigured("this client was built without write access")
        try:
            return self._post_json(self.url(kind), body,
                                   headers=self._headers(content=True))
        except Exception as exc:
            raise EhrUnavailable("could not create %s: %s"
                                 % (kind, type(exc).__name__)) from exc


# ---------------------------------------------------------------------------
# Reducing a resource to what a screen needs
# ---------------------------------------------------------------------------

def patient_summary(resource) -> dict:
    """A Patient resource reduced to a few readable fields.

    FHIR names are a list of structures, any of which may be missing, so every
    step is defensive — a patient with no name must render as "unknown", not as a
    stack trace in front of a clinician.
    """
    if not isinstance(resource, dict):
        return {"name": None, "birth_date": None, "gender": None, "id": None}
    name = None
    names = resource.get("name") or []
    if isinstance(names, list) and names:
        first = names[0] if isinstance(names[0], dict) else {}
        if first.get("text"):
            name = first["text"]
        else:
            given = " ".join(first.get("given") or [])
            family = first.get("family") or ""
            name = (given + " " + family).strip() or None
    return {
        "id": resource.get("id"),
        "name": name,
        "birth_date": resource.get("birthDate"),
        "gender": resource.get("gender"),
    }


def encounter_summary(resource) -> dict:
    """An Encounter reduced to status and when it started."""
    if not isinstance(resource, dict):
        return {"id": None, "status": None, "start": None}
    period = resource.get("period") or {}
    return {
        "id": resource.get("id"),
        "status": resource.get("status"),
        "start": period.get("start") if isinstance(period, dict) else None,
    }


# ---------------------------------------------------------------------------
# Writing a note back into the chart — phase 2.
#
# This is the half that is not reversible. A read that goes wrong shows a
# clinician a blank field; a write that goes wrong puts a permanent entry in
# someone's medical record, and taking it back is a job for the health system's
# own staff, not for a delete button here. So everything below refuses rather
# than guesses, and the caller is expected to have had a human approve the text.
# ---------------------------------------------------------------------------

LOINC_SYSTEM = "http://loinc.org"

# LOINC 11506-3 "Progress note". A recap of one session is a progress note, not a
# consult note. It is a DEFAULT and not a constant because each health system
# maps document types in its own build — a code a customer has not mapped is
# refused at write time, so this has to be settable per deployment.
NOTE_TYPE_CODE = "11506-3"
NOTE_TYPE_DISPLAY = "Progress note"

# FHIR's own id rule (R4 §Resource.id). Enforced because these ids come from the
# EHR and then get pasted into a reference string: an id carrying a slash would
# silently change which resource we claim to be writing against.
_FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


def valid_fhir_id(value) -> bool:
    return bool(_FHIR_ID.match(str(value or "")))


def document_reference_body(*, note_text, patient_id, encounter_id=None,
                            author=None, now=None,
                            type_code=NOTE_TYPE_CODE,
                            type_display=NOTE_TYPE_DISPLAY,
                            content_type="text/plain") -> dict:
    """The DocumentReference we ask the EHR to create.

    A pure function returning a dict, so the exact bytes we would send are
    assertable in a test without a network or a token. That matters more here
    than anywhere else in this file: this is the shape of a permanent medical
    record entry.

    The note travels base64-encoded inside content.attachment.data, which is how
    FHIR carries document bodies — `contentType` says how to read it back.
    """
    text = (note_text or "").strip()
    if not text:
        # A blank progress note in a chart is worse than no note: it looks like
        # the clinician documented the session and found nothing to say.
        raise EhrRefused("refusing to write an empty note to a chart")
    if not valid_fhir_id(patient_id):
        raise EhrRefused("refusing to write a note without a usable patient id")
    if encounter_id and not valid_fhir_id(encounter_id):
        raise EhrRefused("encounter id is not a usable FHIR id")

    body = {
        "resourceType": "DocumentReference",
        "status": "current",
        # docStatus final, not preliminary: a clinician read this text and
        # pressed the button. Calling it preliminary would misdescribe that.
        "docStatus": "final",
        "type": {
            "coding": [{"system": LOINC_SYSTEM, "code": type_code,
                        "display": type_display}],
            "text": type_display,
        },
        "subject": {"reference": "Patient/" + str(patient_id)},
        "content": [{
            "attachment": {
                "contentType": content_type,
                "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            }
        }],
    }
    if now is not None:
        body["date"] = now.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z")
    if encounter_id:
        body["context"] = {
            "encounter": [{"reference": "Encounter/" + str(encounter_id)}]
        }
    if author:
        # fhirUser from the token response is an absolute URL to a Practitioner,
        # which is a legal Reference.reference — passed through as given rather
        # than reassembled, because we did not build it and cannot improve it.
        body["author"] = [{"reference": str(author)}]
    return body


def created_reference(response) -> str:
    """Where the EHR says it put the thing we created, or "".

    A FHIR create answers 201 with a Location header, and the body is allowed to
    be EMPTY. So "no JSON came back" is a success here, not a failure, and the
    transport hands us a dict carrying the status and the location instead. Get
    this wrong and a clinician sees an error for a note that landed, presses the
    button again, and the chart gets two.
    """
    if not isinstance(response, dict):
        return ""
    loc = response.get("_location") or response.get("location") or ""
    if loc:
        return str(loc)
    if response.get("resourceType") == "DocumentReference" and response.get("id"):
        return "DocumentReference/" + str(response["id"])
    return ""


def write_note(*, client, note_text, patient_id, encounter_id=None, author=None,
               now=None, type_code=NOTE_TYPE_CODE,
               type_display=NOTE_TYPE_DISPLAY) -> dict:
    """Build the note and create it. Returns what the EHR said about it.

    Thin on purpose — the decisions are in `document_reference_body`, which is
    testable without a transport, and the sending is in `FhirClient.create`,
    which refuses outright when it was built read-only.
    """
    body = document_reference_body(
        note_text=note_text, patient_id=patient_id, encounter_id=encounter_id,
        author=author, now=now, type_code=type_code, type_display=type_display)
    response = client.create("DocumentReference", body)
    return {"reference": created_reference(response), "sent": body}


# ---------------------------------------------------------------------------
# The flow.
#
# Both halves live here rather than in the route, so the sequence itself is
# testable by calling a function with fake transports — and so a second vendor
# does not mean a second copy of it inside another view.
# ---------------------------------------------------------------------------

def start_launch(*, iss, launch, redirect_uri, scope, tenant_for, fetch_json):
    """Steps 1-3. Returns what the caller must remember, and where to send them.

    The tenant lookup runs FIRST and before any network call. That ordering is
    the security property: an issuer we do not trust must not even receive a
    request from us, because that request carries our client id.
    """
    tenant = tenant_for(iss)                    # raises EhrRefused / EhrNotConfigured

    try:
        doc = fetch_json(smart_config_url(tenant["iss"]), headers=None)
    except EhrError:
        raise
    except Exception as exc:
        raise EhrUnavailable("discovery failed: %s" % type(exc).__name__) from exc
    endpoints = endpoints_from_config(doc)

    verifier = new_verifier()
    state = new_state()
    return {
        "redirect_to": authorize_url(
            authorize_endpoint=endpoints["authorize"],
            client_id=tenant["client_id"],
            redirect_uri=redirect_uri,
            scope=scope,
            state=state,
            iss=tenant["iss"],
            launch=launch,
            code_challenge=challenge_for(verifier),
        ),
        "state": state,
        "verifier": verifier,
        "token_url": endpoints["token"],
        "iss": tenant["iss"],
        "vendor": vendor_for_iss(tenant["iss"]),
    }


def finish_launch(*, code, state, expected_state, verifier, iss, token_url,
                  redirect_uri, tenant_for, fetch_json, post_form,
                  post_json=None):
    """Steps 4-5. Returns the patient and encounter, reduced for display.

    Nothing is persisted HERE — this function still touches no database, which is
    what keeps the flow testable by calling it.

    Phase 2's caller does persist, and the question the phase 1 version of this
    docstring left open has been answered on purpose: FHIR IDS ONLY. The patient
    id and encounter id are kept, because a note cannot be addressed without
    them. The name, the date of birth and the gender are NOT, because keeping
    them would quietly turn this database into a patient index, which is a
    different product with different obligations.
    """
    if not (code and state and expected_state and verifier and iss and token_url):
        raise EhrRefused("callback is missing something a launch would have left")
    if not state_matches(state, expected_state):
        raise EhrRefused("callback state did not match a launch we started")

    # Re-checked, not trusted from the session. Cheap, and it means a tenant
    # switched off between launch and callback stops working immediately.
    tenant = tenant_for(iss)
    auth = tenant["auth"]()

    body = token_request_body(code=code, redirect_uri=redirect_uri,
                              code_verifier=verifier)
    body.update(auth.get("body") or {})
    headers = {"Accept": "application/json"}
    headers.update(auth.get("headers") or {})

    try:
        payload = post_form(token_url, body, headers=headers)
    except Exception as exc:
        raise EhrUnavailable("token exchange failed: %s"
                             % type(exc).__name__) from exc
    ctx = context_from_token(payload)
    fhir_user, fhir_user_verified = verified_identity(
        id_token=ctx["id_token"], iss=tenant["iss"], audience=tenant["client_id"],
        fallback_fhir_user=ctx["fhir_user"], fetch_json=fetch_json)

    client = FhirClient(iss=tenant["iss"], token=ctx["access_token"],
                        fetch_json=fetch_json, post_json=post_json)

    patient = patient_summary(None)
    encounter = encounter_summary(None)
    if ctx["patient"]:
        patient = patient_summary(client.read("Patient", ctx["patient"]))
    if ctx["encounter"]:
        encounter = encounter_summary(client.read("Encounter", ctx["encounter"]))

    return {
        "patient": patient,
        "encounter": encounter,
        "scope": ctx["scope"],
        "vendor": vendor_for_iss(tenant["iss"]),
        "client": client,          # phase 2 writes through this
        # --- what phase 2 needs to write a note later, once the session has
        # actually happened. The ids are duplicated out of `patient` and
        # `encounter` so a caller storing them never has to reach into a dict
        # shaped for a screen.
        "iss": tenant["iss"],
        "patient_id": ctx["patient"] or None,
        "encounter_id": ctx["encounter"] or None,
        "access_token": ctx["access_token"],
        "expires_in": ctx["expires_in"],
        "fhir_user": fhir_user,
        "fhir_user_verified": fhir_user_verified,
    }


# ---------------------------------------------------------------------------
# The one place that knows a vendor apart.
#
# Epic and Oracle Health both speak SMART on FHIR, so the flow above is shared.
# Anything that genuinely differs belongs here and nowhere else, or the
# difference leaks into the flow and doubles it.
# ---------------------------------------------------------------------------

VENDORS = {
    "epic": {
        "label": "Epic",
        "sandbox_iss": "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
    },
    "oracle": {
        "label": "Oracle Health",
        # Not registered yet. Present so adding it is configuration, not surgery.
        "sandbox_iss": "",
    },
}


def vendor_label(vendor: str) -> str:
    return (VENDORS.get(vendor) or {}).get("label") or "EHR"


def vendor_for_iss(iss: str) -> str:
    """Which vendor an issuer belongs to, or "" when we cannot tell.

    Used for wording on screen and in logs and NEVER for a security decision —
    the tenant lookup does that. A heuristic is acceptable here for exactly that
    reason; it would not be acceptable one line further up.
    """
    base = normalise_iss(iss).lower()
    for key, spec in VENDORS.items():
        sandbox = normalise_iss(spec.get("sandbox_iss") or "").lower()
        if sandbox and base == sandbox:
            return key
    if "epic.com" in base:
        return "epic"
    if "cerner.com" in base or "oracle" in base:
        return "oracle"
    return ""
