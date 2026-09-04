"""
tests/test_ehr_launch.py
------------------------
SMART on FHIR launch out of an EHR — phase 1.

No network: every HTTP call the routes make is faked, so the suite tests our
rules rather than Epic's uptime.

Two things get harder scrutiny than the rest, because they are the parts that
would matter if they were wrong:

  * the ISSUER ALLOWLIST. /ehr/launch is handed a FHIR base URL by whoever opens
    the link and then trusts it — for discovery, and to post an authorization
    code to. Get this wrong and a crafted launch harvests our client id and a
    live code.
  * NOTHING IS STORED. Whether patient identity belongs in this app is a real
    decision; a spike must not settle it by writing a row.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import base64
import hashlib
import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-ehr")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

import config
import ehr
from TogetherMindsAI import app
import routes_ehr
from models import db, init_encryption, AuditLog

init_encryption(TEST_KEY)

ISS = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
AUTHZ = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize"
TOKEN = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token"
CLIENT_ID = "edca08cc-0aee-4f3c-83be-1f832255f406"


@pytest.fixture
def client():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    db._app_engines[app] = {None: engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def _on(**over):
    """Turn the integration on for one test."""
    values = {"EHR_ENABLED": True, "EPIC_CLIENT_ID": CLIENT_ID,
              "EPIC_SANDBOX_CLIENT_SECRET": "sandbox-secret",
              "EHR_ALLOWED_ISS": (ISS,),
              "EHR_SCOPES": "launch openid fhirUser patient/Patient.read"}
    values.update(over)
    return [patch.object(config, k, v) for k, v in values.items()]


class _Enabled:
    """Context manager: config patched on for the block."""
    def __init__(self, **over):
        self._patches = _on(**over)

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


SMART_DOC = {"authorization_endpoint": AUTHZ, "token_endpoint": TOKEN,
             "capabilities": ["launch-ehr"]}

TOKEN_OK = {"access_token": "at-123", "token_type": "Bearer", "expires_in": 3600,
            "patient": "ePat1", "encounter": "eEnc1",
            "scope": "launch patient/Patient.read"}

PATIENT = {"resourceType": "Patient", "id": "ePat1", "birthDate": "1980-04-01",
           "gender": "female",
           "name": [{"given": ["Camila", "Maria"], "family": "Lopez"}]}

ENCOUNTER = {"resourceType": "Encounter", "id": "eEnc1", "status": "finished",
             "period": {"start": "2026-09-01T14:00:00Z"}}


# ---------------------------------------------------------------------------
# Who we will talk to — the part that matters most
# ---------------------------------------------------------------------------

def test_the_configured_issuer_is_accepted():
    assert ehr.issuer_allowed(ISS, (ISS,)) is True
    # A trailing slash is the same server.
    assert ehr.issuer_allowed(ISS + "/", (ISS,)) is True


def test_an_unknown_issuer_is_refused():
    assert ehr.issuer_allowed("https://evil.example/api/FHIR/R4", (ISS,)) is False
    assert ehr.issuer_allowed("", (ISS,)) is False
    assert ehr.issuer_allowed(ISS, ()) is False


def test_a_lookalike_issuer_is_refused():
    """The reason the check is an exact match and not a prefix or suffix test.

    Each of these defeats one of the careless versions:
      * `?u=<allowed>` ENDS WITH the allowed base, so `endswith` lets it through
      * `<allowed>.attacker.example` STARTS WITH it, so `startswith` does
      * the others read as Epic to a human but resolve elsewhere
    """
    for hostile in (
        "https://attacker.example/?u=" + ISS,                   # defeats endswith
        "https://attacker.example/" + ISS,                      # defeats endswith
        ISS + ".attacker.example",                              # defeats startswith
        ISS + "/../../../evil",                                 # defeats startswith
        "https://fhir.epic.com.attacker.example/api/FHIR/R4",
        "https://fhir.epic.com@attacker.example/api/FHIR/R4",   # userinfo trick
    ):
        assert ehr.issuer_allowed(hostile, (ISS,)) is False, hostile


def test_a_plain_http_issuer_is_refused():
    """The token exchange carries a client secret."""
    plain = ISS.replace("https://", "http://")
    assert ehr.issuer_allowed(plain, (ISS, plain)) is False


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def test_the_challenge_is_the_sha256_of_the_verifier():
    v = ehr.new_verifier()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    assert ehr.challenge_for(v) == expected


def test_the_challenge_carries_no_padding():
    """RFC 7636 says unpadded, and Epic refuses a challenge containing '='."""
    for _ in range(20):
        assert "=" not in ehr.challenge_for(ehr.new_verifier())


def test_every_launch_gets_a_fresh_verifier_and_state():
    assert ehr.new_verifier() != ehr.new_verifier()
    assert ehr.new_state() != ehr.new_state()
    assert 43 <= len(ehr.new_verifier()) <= 128      # RFC 7636 range


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_the_endpoints_are_read_from_the_smart_configuration():
    out = ehr.endpoints_from_config(SMART_DOC)
    assert out == {"authorize": AUTHZ, "token": TOKEN}


def test_a_configuration_missing_an_endpoint_is_refused():
    """Guessing a conventional path would mean sending a clinician, and an
    authorization code, somewhere the server never advertised."""
    with pytest.raises(ValueError) as e:
        ehr.endpoints_from_config({"authorization_endpoint": AUTHZ})
    assert "token_endpoint" in str(e.value)
    with pytest.raises(ValueError):
        ehr.endpoints_from_config({"token_endpoint": TOKEN})
    with pytest.raises(ValueError):
        ehr.endpoints_from_config("not json")


# ---------------------------------------------------------------------------
# The authorize redirect
# ---------------------------------------------------------------------------

def _params(url):
    import urllib.parse
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def test_the_authorize_url_carries_everything_the_ehr_needs():
    url = ehr.authorize_url(
        authorize_endpoint=AUTHZ, client_id=CLIENT_ID,
        redirect_uri="https://tm.onlinegbc.com/ehr/callback",
        scope="launch openid", state="st", iss=ISS, launch="lk1",
        code_challenge="ch")
    p = _params(url)
    assert p["response_type"] == "code"
    assert p["client_id"] == CLIENT_ID
    assert p["redirect_uri"] == "https://tm.onlinegbc.com/ehr/callback"
    assert p["state"] == "st"
    assert p["launch"] == "lk1"
    assert p["code_challenge"] == "ch"
    assert p["code_challenge_method"] == "S256"


def test_the_authorize_url_names_the_audience():
    """`aud` tells the authorization server which resource server the token is
    for, so a token minted for one EHR cannot be replayed against another."""
    url = ehr.authorize_url(
        authorize_endpoint=AUTHZ, client_id=CLIENT_ID, redirect_uri="https://x/cb",
        scope="launch", state="st", iss=ISS + "/", launch="lk1", code_challenge="ch")
    assert _params(url)["aud"] == ISS          # normalised, no trailing slash


def test_an_authorize_endpoint_that_already_has_a_query_still_works():
    url = ehr.authorize_url(
        authorize_endpoint=AUTHZ + "?tenant=abc", client_id=CLIENT_ID,
        redirect_uri="https://x/cb", scope="launch", state="st", iss=ISS,
        launch="", code_challenge="ch")
    p = _params(url)
    assert p["tenant"] == "abc" and p["client_id"] == CLIENT_ID
    assert "launch" not in p                   # no patient context, so not sent


# ---------------------------------------------------------------------------
# The token exchange
# ---------------------------------------------------------------------------

def test_the_exchange_sends_the_verifier_not_the_challenge():
    body = ehr.token_request_body(code="c1", redirect_uri="https://x/cb",
                                  client_id=CLIENT_ID, code_verifier="v1")
    assert body["code_verifier"] == "v1"
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "c1"
    # Repeated on purpose: the server compares it, which is what stops a code
    # being redeemed against a different redirect.
    assert body["redirect_uri"] == "https://x/cb"


def test_the_secret_travels_in_the_header_not_the_body():
    """A secret in a form body ends up in more logs than one in a header."""
    body = ehr.token_request_body(code="c1", redirect_uri="https://x/cb",
                                  client_id=CLIENT_ID, code_verifier="v1")
    assert "client_secret" not in body
    header = ehr.basic_auth_header(CLIENT_ID, "shh")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    assert decoded == CLIENT_ID + ":shh"


def test_a_public_client_sends_no_authorization_header():
    assert ehr.basic_auth_header(CLIENT_ID, "") is None


def test_the_token_response_is_read_for_context():
    ctx = ehr.context_from_token(TOKEN_OK)
    assert ctx["access_token"] == "at-123"
    assert ctx["patient"] == "ePat1"
    assert ctx["encounter"] == "eEnc1"


def test_a_token_response_with_no_token_is_refused():
    with pytest.raises(ValueError):
        ehr.context_from_token({"patient": "ePat1"})
    with pytest.raises(ValueError):
        ehr.context_from_token("not json")


def test_a_launch_with_no_patient_is_not_an_error():
    """Normal for a launch opened outside a chart."""
    ctx = ehr.context_from_token({"access_token": "at"})
    assert ctx["patient"] is None and ctx["encounter"] is None


# ---------------------------------------------------------------------------
# Reading a resource
# ---------------------------------------------------------------------------

def test_a_resource_id_cannot_change_which_endpoint_is_called():
    """The id comes from the EHR. A slash or a dot-dot in it must not escape."""
    url = ehr.resource_url(ISS, "Patient", "../../metadata")
    assert url == ISS + "/Patient/..%2F..%2Fmetadata"
    assert url.count("/Patient/") == 1


def test_a_patient_is_reduced_to_what_the_page_shows():
    out = ehr.patient_summary(PATIENT)
    assert out["name"] == "Camila Maria Lopez"
    assert out["birth_date"] == "1980-04-01"
    assert out["id"] == "ePat1"


def test_a_patient_with_a_text_name_uses_it():
    out = ehr.patient_summary({"name": [{"text": "Lopez, Camila"}]})
    assert out["name"] == "Lopez, Camila"


def test_a_patient_with_no_name_renders_rather_than_crashing():
    """A missing name must not put a stack trace in front of a clinician."""
    for odd in ({}, {"name": []}, {"name": [{}]}, {"name": "nonsense"}, None):
        out = ehr.patient_summary(odd)
        assert out["name"] is None


def test_an_encounter_is_reduced_to_status_and_start():
    out = ehr.encounter_summary(ENCOUNTER)
    assert out["status"] == "finished"
    assert out["start"] == "2026-09-01T14:00:00Z"
    assert ehr.encounter_summary({"period": "nonsense"})["start"] is None


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

def test_both_routes_are_invisible_while_the_flag_is_off(client):
    """Nothing about this appears in production until it is switched on."""
    with patch.object(config, "EHR_ENABLED", False):
        assert client.get("/ehr/launch?iss=" + ISS).status_code == 404
        assert client.get("/ehr/callback?code=c&state=s").status_code == 404


def test_a_launch_from_an_unknown_issuer_never_reaches_the_network(client):
    """The allowlist has to be checked BEFORE discovery, or the crafted server
    already has a request from us."""
    with _Enabled(), patch.object(routes_ehr, "_http_get_json") as fetched:
        rv = client.get("/ehr/launch?iss=https://evil.example/api/FHIR/R4&launch=x")
    assert rv.status_code == 400
    fetched.assert_not_called()


def test_a_good_launch_redirects_to_the_ehr(client):
    with _Enabled(), patch.object(routes_ehr, "_http_get_json",
                                  return_value=SMART_DOC):
        rv = client.get("/ehr/launch?iss=" + ISS + "&launch=lk1")
    assert rv.status_code == 302
    p = _params(rv.headers["Location"])
    assert rv.headers["Location"].startswith(AUTHZ)
    assert p["client_id"] == CLIENT_ID
    assert p["launch"] == "lk1"
    assert p["aud"] == ISS
    assert p["code_challenge_method"] == "S256"
    assert p["redirect_uri"].endswith("/ehr/callback")


def test_a_launch_with_no_client_id_configured_says_so(client):
    with _Enabled(EPIC_CLIENT_ID=""), \
         patch.object(routes_ehr, "_http_get_json", return_value=SMART_DOC):
        rv = client.get("/ehr/launch?iss=" + ISS)
    assert rv.status_code == 503


def test_a_launch_survives_discovery_being_down(client):
    """A clinician gets a clear failure, not a traceback."""
    with _Enabled(), patch.object(routes_ehr, "_http_get_json",
                                  side_effect=RuntimeError("connection reset")):
        rv = client.get("/ehr/launch?iss=" + ISS)
    assert rv.status_code == 502


def _complete_launch(client):
    """Run the launch so the session holds state, and return that state."""
    with patch.object(routes_ehr, "_http_get_json", return_value=SMART_DOC):
        rv = client.get("/ehr/launch?iss=" + ISS + "&launch=lk1")
    return _params(rv.headers["Location"])["state"]


def test_the_whole_flow_reads_the_patient(client):
    with _Enabled():
        state = _complete_launch(client)
        with patch.object(routes_ehr, "_http_post_form", return_value=TOKEN_OK), \
             patch.object(routes_ehr, "_http_get_json",
                          side_effect=[PATIENT, ENCOUNTER]):
            rv = client.get("/ehr/callback?code=c1&state=" + state)
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "Camila Maria Lopez" in body
    assert "1980-04-01" in body
    assert "finished" in body


def test_the_callback_refuses_a_state_it_did_not_issue(client):
    """A callback that does not match a launch we started is a stale tab or a
    forged request."""
    with _Enabled():
        _complete_launch(client)
        with patch.object(routes_ehr, "_http_post_form") as posted:
            rv = client.get("/ehr/callback?code=c1&state=not-the-one")
    assert rv.status_code == 400
    posted.assert_not_called()


def test_the_callback_refuses_when_there_was_no_launch(client):
    """Hitting the callback cold — no state in the session to match."""
    with _Enabled(), patch.object(routes_ehr, "_http_post_form") as posted:
        rv = client.get("/ehr/callback?code=c1&state=whatever")
    assert rv.status_code == 400
    posted.assert_not_called()


def test_the_state_is_single_use(client):
    """Replaying a callback must not work a second time."""
    with _Enabled():
        state = _complete_launch(client)
        with patch.object(routes_ehr, "_http_post_form", return_value=TOKEN_OK), \
             patch.object(routes_ehr, "_http_get_json",
                          side_effect=[PATIENT, ENCOUNTER]):
            first = client.get("/ehr/callback?code=c1&state=" + state)
        with patch.object(routes_ehr, "_http_post_form") as posted:
            second = client.get("/ehr/callback?code=c1&state=" + state)
    assert first.status_code == 200
    assert second.status_code == 400
    posted.assert_not_called()


def test_the_callback_reports_an_ehr_refusal(client):
    """The EHR can answer with an error instead of a code — a clinician without
    rights, or a cancelled prompt.

    The log line is asserted, not just the 400: a missing code 400s anyway, so
    without this the test would pass even with the error branch removed and we
    would have no idea WHY a launch failed in production.
    """
    with _Enabled():
        _complete_launch(client)
        with patch.object(app.logger, "warning") as warned:
            rv = client.get("/ehr/callback?error=access_denied&state=x")
    assert rv.status_code == 400
    said = " ".join(str(c) for c in warned.call_args_list)
    assert "access_denied" in said


def test_the_callback_survives_a_failed_token_exchange(client):
    with _Enabled():
        state = _complete_launch(client)
        with patch.object(routes_ehr, "_http_post_form",
                          side_effect=RuntimeError("400 invalid_grant")):
            rv = client.get("/ehr/callback?code=c1&state=" + state)
    assert rv.status_code == 502


def test_the_exchange_sends_the_secret_and_the_verifier(client):
    with _Enabled():
        state = _complete_launch(client)
        with patch.object(routes_ehr, "_http_post_form",
                          return_value=TOKEN_OK) as posted, \
             patch.object(routes_ehr, "_http_get_json",
                          side_effect=[PATIENT, ENCOUNTER]):
            client.get("/ehr/callback?code=c1&state=" + state)
    kwargs = posted.call_args
    body = kwargs.args[1] if len(kwargs.args) > 1 else kwargs.kwargs["data"]
    headers = kwargs.kwargs.get("headers") or kwargs.args[2]
    assert body["code_verifier"]
    assert body["code"] == "c1"
    assert headers["Authorization"].startswith("Basic ")


def test_a_launch_with_no_patient_still_renders(client):
    with _Enabled():
        state = _complete_launch(client)
        with patch.object(routes_ehr, "_http_post_form",
                          return_value={"access_token": "at"}), \
             patch.object(routes_ehr, "_http_get_json") as read:
            rv = client.get("/ehr/callback?code=c1&state=" + state)
    assert rv.status_code == 200
    assert "carried no encounter" in rv.get_data(as_text=True)
    read.assert_not_called()          # nothing to read without an id


# ---------------------------------------------------------------------------
# Nothing is stored
# ---------------------------------------------------------------------------

def test_no_patient_data_reaches_the_database(client):
    """Whether patient identity belongs in this app is a real decision. A spike
    must not settle it by writing a row."""
    from sqlalchemy import text as _sql
    with _Enabled():
        state = _complete_launch(client)
        with patch.object(routes_ehr, "_http_post_form", return_value=TOKEN_OK), \
             patch.object(routes_ehr, "_http_get_json",
                          side_effect=[PATIENT, ENCOUNTER]):
            client.get("/ehr/callback?code=c1&state=" + state)
    with app.app_context():
        names = [r[0] for r in db.session.execute(_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")).all()]
        for table in names:
            rows = db.session.execute(_sql(
                "SELECT * FROM %s" % table)).mappings().all()
            blob = repr(rows)
            assert "Camila" not in blob, table
            assert "1980-04-01" not in blob, table
            assert "ePat1" not in blob, table


def test_the_audit_log_records_the_launch_without_any_pii(client):
    """Launches are worth recording. Names and dates of birth are not."""
    with _Enabled():
        state = _complete_launch(client)
        with patch.object(routes_ehr, "_http_post_form", return_value=TOKEN_OK), \
             patch.object(routes_ehr, "_http_get_json",
                          side_effect=[PATIENT, ENCOUNTER]):
            client.get("/ehr/callback?code=c1&state=" + state)
    with app.app_context():
        rows = AuditLog.query.filter(
            AuditLog.event_type.in_(("ehr_launch_started",
                                     "ehr_launch_completed"))).all()
        assert len(rows) == 2
        for row in rows:
            assert "Camila" not in (row.details or "")
            assert "ePat1" not in (row.details or "")
            assert "1980" not in (row.details or "")
        done = [r for r in rows if r.event_type == "ehr_launch_completed"][0]
        assert "epic" in done.details            # vendor, which is not PII


def test_the_access_token_is_not_written_anywhere(client):
    from sqlalchemy import text as _sql
    with _Enabled():
        state = _complete_launch(client)
        with patch.object(routes_ehr, "_http_post_form", return_value=TOKEN_OK), \
             patch.object(routes_ehr, "_http_get_json",
                          side_effect=[PATIENT, ENCOUNTER]):
            client.get("/ehr/callback?code=c1&state=" + state)
    with app.app_context():
        rows = db.session.execute(_sql("SELECT * FROM audit_logs")).mappings().all()
        assert "at-123" not in repr(rows)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_the_routes_are_attached_from_their_own_module():
    for endpoint in ("ehr_launch", "ehr_callback"):
        assert endpoint in app.view_functions, endpoint
        assert app.view_functions[endpoint].__module__ == "routes_ehr", endpoint


def test_the_allowlist_defaults_to_the_epic_sandbox_only():
    """A default of "anything" would be the one mistake that matters here."""
    assert config.EHR_ALLOWED_ISS
    assert all(a.startswith("https://") for a in config.EHR_ALLOWED_ISS)
    assert "epic.com" in config.EHR_ALLOWED_ISS[0]


def test_the_integration_is_off_unless_switched_on():
    """Read from a clean environment: the flag must default to off."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "config.py"),
               encoding="utf-8").read()
    assert 'os.environ.get("EHR_ENABLED", "false")' in src


def test_a_vendor_is_named_for_wording_only():
    assert ehr.vendor_for_iss(ISS) == "epic"
    assert ehr.vendor_for_iss("https://fhir-ehr.cerner.com/r4") == "oracle"
    assert ehr.vendor_for_iss("https://something.else/api") == ""
