"""
ehr.py
------
SMART on FHIR — launching TogetherMindsAI from inside an EHR (Epic, Oracle Health).

Flask-free on purpose, like billing.py and admin_access.py, so every rule here is
directly testable without a request: which issuers are allowed, how the authorize
URL is built, what the token exchange sends, and how a FHIR resource is read. The
HTTP routes live in routes_ehr.py.

The flow, once, so the pieces below make sense:

  1. The EHR opens our launch URL with `iss` (its FHIR base) and `launch` (an
     opaque handle for "the patient currently open").
  2. We ask that base for its SMART configuration to learn where to send the
     clinician and where to exchange the code.
  3. We redirect them to the EHR's authorize endpoint. It already knows who they
     are — they are signed into the EHR — so they are usually bounced straight
     back.
  4. The EHR calls our callback with a code. We exchange it server-side for an
     access token, and the token response also names the patient.
  5. We read that patient with the token.

Two things are load-bearing rather than decoration:

  * `iss` arrives in a query string and is then TRUSTED — we fetch configuration
    from it and post an authorization code to it. An attacker who can choose it
    can harvest both. `issuer_allowed` is the gate, and nothing here talks to a
    base that has not passed it.
  * PKCE. The authorization code travels through the clinician's browser. Without
    a verifier, anyone who intercepts it can redeem it.
"""

import base64
import hashlib
import logging
import secrets
import urllib.parse

log = logging.getLogger(__name__)

# How long to wait on an EHR. Short: a clinician is staring at a blank tab, and a
# hung discovery call is worse than a clear failure.
TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Who we are willing to talk to
# ---------------------------------------------------------------------------

def normalise_iss(iss: str) -> str:
    """A FHIR base URL in the one form we compare. Trailing slashes only."""
    return (iss or "").strip().rstrip("/")


def issuer_allowed(iss: str, allowed) -> bool:
    """True when this issuer is one we are configured to trust.

    An exact match after normalising, deliberately — NOT a prefix or "endswith"
    test. `https://fhir.epic.com.attacker.example` ends with nothing useful, but
    a careless prefix check on `https://fhir.epic.com` would pass
    `https://fhir.epic.com@attacker.example`, and a careless suffix check would
    pass the first. Exact is the only version with no clever way around it.

    https is required. The token exchange carries a client secret.
    """
    base = normalise_iss(iss)
    if not base or not base.lower().startswith("https://"):
        return False
    return base in {normalise_iss(a) for a in (allowed or ())}


# ---------------------------------------------------------------------------
# PKCE
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


# ---------------------------------------------------------------------------
# SMART discovery
# ---------------------------------------------------------------------------

def smart_config_url(iss: str) -> str:
    return normalise_iss(iss) + "/.well-known/smart-configuration"


def endpoints_from_config(doc) -> dict:
    """{authorize, token} from a SMART configuration document.

    Raises ValueError when either is missing. A launch cannot proceed without
    both, and guessing conventional paths would mean sending a clinician — and an
    authorization code — somewhere the server never advertised.
    """
    if not isinstance(doc, dict):
        raise ValueError("SMART configuration was not a JSON object.")
    authorize = (doc.get("authorization_endpoint") or "").strip()
    token = (doc.get("token_endpoint") or "").strip()
    if not authorize or not token:
        missing = []
        if not authorize:
            missing.append("authorization_endpoint")
        if not token:
            missing.append("token_endpoint")
        raise ValueError("SMART configuration is missing: " + ", ".join(missing))
    return {"authorize": authorize, "token": token}


# ---------------------------------------------------------------------------
# Step 3 — where to send the clinician
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
# Step 4 — the token exchange
# ---------------------------------------------------------------------------

def token_request_body(*, code, redirect_uri, client_id, code_verifier) -> dict:
    """Form fields for the code-for-token exchange.

    `redirect_uri` is repeated here even though the code came back to it. The
    spec requires it, and the server compares the two — which is what stops a
    code issued for our app being redeemed against a different redirect.

    No client secret in here: it goes in the Authorization header instead (see
    basic_auth_header), because a secret in a form body ends up in more logs.
    """
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }


def basic_auth_header(client_id: str, client_secret: str):
    """HTTP Basic credentials for a confidential client, or None when we have no
    secret — a public client sends client_id in the body alone."""
    if not client_secret:
        return None
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def context_from_token(payload) -> dict:
    """What the token response tells us, in the shape the rest of the code wants.

    The patient id arrives as `patient` — a context field, not a scope — and its
    absence is the normal case for a launch with no patient open, so it is
    reported as None rather than raised.
    """
    if not isinstance(payload, dict):
        raise ValueError("Token response was not a JSON object.")
    token = (payload.get("access_token") or "").strip()
    if not token:
        raise ValueError("Token response carried no access_token.")
    return {
        "access_token": token,
        "token_type": payload.get("token_type") or "Bearer",
        "expires_in": payload.get("expires_in"),
        "patient": payload.get("patient") or None,
        "encounter": payload.get("encounter") or None,
        "scope": payload.get("scope") or "",
        # Who the clinician is, when the server sends it.
        "fhir_user": payload.get("fhirUser") or payload.get("fhir_user") or None,
    }


# ---------------------------------------------------------------------------
# Step 5 — reading a resource
# ---------------------------------------------------------------------------

def resource_url(iss: str, kind: str, resource_id: str) -> str:
    """The URL for one FHIR resource.

    The id is percent-encoded: it comes from the EHR, and an id containing a
    slash or a dot-dot would otherwise change which endpoint we call.
    """
    return "%s/%s/%s" % (normalise_iss(iss), kind,
                         urllib.parse.quote(str(resource_id or ""), safe=""))


def patient_summary(resource) -> dict:
    """A Patient resource reduced to what the spike shows on screen.

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


def vendor_for_iss(iss: str) -> str:
    """Which vendor an issuer belongs to, or "" when we cannot tell. Only used
    for wording on screen and in logs — never for a security decision."""
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
