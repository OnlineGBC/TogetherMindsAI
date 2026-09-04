"""
tests/test_ehr_launch.py
------------------------
SMART on FHIR launch out of an EHR — phase 1.

No network anywhere: the transports are plain functions passed in, so the suite
tests our rules and our FLOW rather than Epic's uptime.

Most of this file needs no Flask. That is the point of the design — the sequence
(discover, redirect, exchange, read) lives in ehr.py, so it is exercised by
calling a function. Only the last section uses a test client, and only for what
HTTP actually owns: the feature flag, the session round-trip, and turning an
error into a status code.

Three things get harder scrutiny than the rest, because they are what would
matter if they were wrong:

  * the TENANT LOOKUP. /ehr/launch is handed a FHIR base URL by whoever opens the
    link and then trusts it — for discovery, and to post an authorization code
    to. Get this wrong and a crafted launch harvests our client id and a live
    code.
  * NOTHING IS STORED. Whether patient identity belongs in this app is a real
    decision; a spike must not settle it by writing a row.
  * THE SEAMS hold. Swapping the tenant lookup or the authentication method must
    not require touching the flow, because that is the whole reason they exist.
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
SECRET = "sandbox-secret"
REDIRECT = "https://tm.onlinegbc.com/ehr/callback"

SMART_DOC = {"authorization_endpoint": AUTHZ, "token_endpoint": TOKEN,
             "capabilities": ["launch-ehr"],
             "code_challenge_methods_supported": ["S256"]}

TOKEN_OK = {"access_token": "at-123", "token_type": "Bearer", "expires_in": 3600,
            "patient": "ePat1", "encounter": "eEnc1",
            "scope": "launch patient/Patient.read"}

PATIENT = {"resourceType": "Patient", "id": "ePat1", "birthDate": "1980-04-01",
           "gender": "female",
           "name": [{"given": ["Camila", "Maria"], "family": "Lopez"}]}

ENCOUNTER = {"resourceType": "Encounter", "id": "eEnc1", "status": "finished",
             "period": {"start": "2026-09-01T14:00:00Z"}}


def _tenant(allowed=(ISS,), client_id=CLIENT_ID, secret=SECRET):
    return ehr.tenant_from_config(
        allowed_iss=allowed, client_id=client_id,
        auth=ehr.secret_auth(client_id, secret))


class _Transport:
    """A recording stand-in for the network. Every call is remembered, so a test
    can assert what we SENT and not only what we did with the answer."""

    def __init__(self, gets=None, posts=None):
        self._gets = list(gets or [])
        self._posts = list(posts or [])
        self.get_calls = []
        self.post_calls = []

    def fetch_json(self, url, headers=None):
        self.get_calls.append({"url": url, "headers": headers})
        if not self._gets:
            raise AssertionError("unexpected GET: " + url)
        nxt = self._gets.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def post_form(self, url, data, headers=None):
        self.post_calls.append({"url": url, "data": data, "headers": headers})
        if not self._posts:
            raise AssertionError("unexpected POST: " + url)
        nxt = self._posts.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _params(url):
    import urllib.parse
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


# ===========================================================================
# Who we are willing to talk to
# ===========================================================================

def test_the_configured_issuer_is_accepted():
    assert ehr.issuer_allowed(ISS, (ISS,)) is True
    assert ehr.issuer_allowed(ISS + "/", (ISS,)) is True      # same server


def test_an_unknown_issuer_is_refused():
    assert ehr.issuer_allowed("https://evil.example/api/FHIR/R4", (ISS,)) is False
    assert ehr.issuer_allowed("", (ISS,)) is False
    assert ehr.issuer_allowed(ISS, ()) is False


def test_a_lookalike_issuer_is_refused():
    """The reason the check is an exact match and not a prefix or suffix test.

    Each of these defeats one of the careless versions:
      * `?u=<allowed>` ENDS WITH the allowed base, so `endswith` lets it through
      * `<allowed>.attacker.example` STARTS WITH it, so `startswith` does
    """
    for hostile in (
        "https://attacker.example/?u=" + ISS,
        "https://attacker.example/" + ISS,
        ISS + ".attacker.example",
        ISS + "/../../../evil",
        "https://fhir.epic.com.attacker.example/api/FHIR/R4",
        "https://fhir.epic.com@attacker.example/api/FHIR/R4",
    ):
        assert ehr.issuer_allowed(hostile, (ISS,)) is False, hostile


def test_a_plain_http_issuer_is_refused():
    """The token exchange carries a credential."""
    plain = ISS.replace("https://", "http://")
    assert ehr.issuer_allowed(plain, (ISS, plain)) is False


def test_the_tenant_lookup_returns_that_customers_credentials():
    out = _tenant()(ISS)
    assert out["iss"] == ISS
    assert out["client_id"] == CLIENT_ID
    assert callable(out["auth"])


def test_the_tenant_lookup_refuses_an_unknown_issuer():
    with pytest.raises(ehr.EhrRefused):
        _tenant()("https://evil.example/api/FHIR/R4")


def test_the_tenant_lookup_says_when_it_is_unconfigured():
    """Different from a refusal: we would proceed, we are just not set up to.
    The route turns one into 400 and the other into 503, and a clinician seeing
    503 tells you it is your configuration and not their EHR."""
    with pytest.raises(ehr.EhrNotConfigured):
        _tenant(client_id="")(ISS)


# ===========================================================================
# The seams — the reason this was refactored
# ===========================================================================

def test_a_different_tenant_lookup_needs_no_change_to_the_flow():
    """The seam that matters. Today one customer from config; tomorrow fifty from
    a table. This is a lookup the flow has never seen, with its own issuer and its
    own client id, and the flow runs unchanged."""
    other_iss = "https://fhir.hospital.example/api/FHIR/R4"

    def from_a_table(iss):
        rows = {other_iss: {"iss": other_iss, "client_id": "client-for-hospital",
                            "auth": ehr.secret_auth("client-for-hospital", "s2")}}
        if iss not in rows:
            raise ehr.EhrRefused("not a customer")
        return rows[iss]

    t = _Transport(gets=[SMART_DOC])
    out = ehr.start_launch(iss=other_iss, launch="lk1", redirect_uri=REDIRECT,
                           scope="launch", tenant_for=from_a_table,
                           fetch_json=t.fetch_json)
    assert _params(out["redirect_to"])["client_id"] == "client-for-hospital"
    assert t.get_calls[0]["url"].startswith(other_iss)


def test_a_different_authentication_method_needs_no_change_to_the_flow():
    """The other seam. Production uses private_key_jwt — Epic's own server
    advertises it — so it has to be addable as a function. This fake one puts a
    client assertion in the BODY instead of a header, which is the shape the real
    one takes, and the flow carries it without knowing."""
    def jwt_like():
        return {"headers": {},
                "body": {"client_assertion": "signed.jwt.here",
                         "client_assertion_type": "urn:ietf:params:oauth:"
                                                  "client-assertion-type:jwt-bearer"}}

    def tenant_for(iss):
        return {"iss": ISS, "client_id": CLIENT_ID, "auth": jwt_like}

    t = _Transport(gets=[PATIENT, ENCOUNTER], posts=[TOKEN_OK])
    ehr.finish_launch(code="c1", state="s", expected_state="s", verifier="v",
                      iss=ISS, token_url=TOKEN, redirect_uri=REDIRECT,
                      tenant_for=tenant_for, fetch_json=t.fetch_json,
                      post_form=t.post_form)
    body = t.post_calls[0]["data"]
    assert body["client_assertion"] == "signed.jwt.here"
    assert "Authorization" not in (t.post_calls[0]["headers"] or {})


def test_the_flow_reaches_no_network_of_its_own():
    """ehr.py must import nothing that can make a request. If it ever does, the
    transports stop being the only door and the tests above stop meaning much."""
    import inspect
    src = inspect.getsource(ehr)
    for banned in ("import requests", "urlopen", "http.client", "from flask"):
        assert banned not in src, banned


# ===========================================================================
# PKCE and state
# ===========================================================================

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
    assert 43 <= len(ehr.new_verifier()) <= 128          # RFC 7636 range


def test_an_absent_state_never_matches():
    """Otherwise a callback with no state would sail past a session with none."""
    assert ehr.state_matches("", "") is False
    assert ehr.state_matches(None, None) is False
    assert ehr.state_matches("abc", None) is False
    assert ehr.state_matches("abc", "abc") is True


# ===========================================================================
# Discovery
# ===========================================================================

def test_the_endpoints_are_read_from_the_smart_configuration():
    assert ehr.endpoints_from_config(SMART_DOC) == {"authorize": AUTHZ,
                                                    "token": TOKEN}


def test_a_configuration_missing_an_endpoint_is_refused():
    """Guessing a conventional path would mean sending a clinician, and an
    authorization code, somewhere the server never advertised."""
    with pytest.raises(ehr.EhrUnavailable) as e:
        ehr.endpoints_from_config({"authorization_endpoint": AUTHZ})
    assert "token_endpoint" in str(e.value)
    with pytest.raises(ehr.EhrUnavailable):
        ehr.endpoints_from_config({"token_endpoint": TOKEN})
    with pytest.raises(ehr.EhrUnavailable):
        ehr.endpoints_from_config("not json")


# ===========================================================================
# The authorize redirect
# ===========================================================================

def test_the_authorize_url_carries_everything_the_ehr_needs():
    url = ehr.authorize_url(
        authorize_endpoint=AUTHZ, client_id=CLIENT_ID, redirect_uri=REDIRECT,
        scope="launch openid", state="st", iss=ISS, launch="lk1",
        code_challenge="ch")
    p = _params(url)
    assert p["response_type"] == "code"
    assert p["client_id"] == CLIENT_ID
    assert p["redirect_uri"] == REDIRECT
    assert p["state"] == "st"
    assert p["launch"] == "lk1"
    assert p["code_challenge"] == "ch"
    assert p["code_challenge_method"] == "S256"


def test_the_authorize_url_names_the_audience():
    """`aud` tells the authorization server which resource server the token is
    for, so a token minted for one EHR cannot be replayed against another."""
    url = ehr.authorize_url(
        authorize_endpoint=AUTHZ, client_id=CLIENT_ID, redirect_uri=REDIRECT,
        scope="launch", state="st", iss=ISS + "/", launch="lk1",
        code_challenge="ch")
    assert _params(url)["aud"] == ISS                    # normalised


def test_an_authorize_endpoint_that_already_has_a_query_still_works():
    url = ehr.authorize_url(
        authorize_endpoint=AUTHZ + "?tenant=abc", client_id=CLIENT_ID,
        redirect_uri=REDIRECT, scope="launch", state="st", iss=ISS,
        launch="", code_challenge="ch")
    p = _params(url)
    assert p["tenant"] == "abc" and p["client_id"] == CLIENT_ID
    assert "launch" not in p                             # no patient context


# ===========================================================================
# Authentication
# ===========================================================================

def test_the_secret_travels_in_the_header_not_the_body():
    """Bodies end up in more logs, proxies and error reports than headers do."""
    applied = ehr.secret_auth(CLIENT_ID, "shh")()
    assert "client_secret" not in applied["body"]
    header = applied["headers"]["Authorization"]
    assert base64.b64decode(header.split(" ", 1)[1]).decode() == CLIENT_ID + ":shh"


def test_a_public_client_sends_no_authorization_header():
    applied = ehr.public_auth(CLIENT_ID)()
    assert applied["headers"] == {}
    assert applied["body"]["client_id"] == CLIENT_ID


def test_the_exchange_sends_the_verifier_not_the_challenge():
    body = ehr.token_request_body(code="c1", redirect_uri=REDIRECT,
                                   code_verifier="v1")
    assert body["code_verifier"] == "v1"
    assert body["grant_type"] == "authorization_code"
    # Repeated on purpose: the server compares it, which is what stops a code
    # being redeemed against a different redirect.
    assert body["redirect_uri"] == REDIRECT


def test_the_token_response_is_read_for_context():
    ctx = ehr.context_from_token(TOKEN_OK)
    assert ctx["access_token"] == "at-123"
    assert ctx["patient"] == "ePat1" and ctx["encounter"] == "eEnc1"


def test_a_token_response_with_no_token_is_refused():
    with pytest.raises(ehr.EhrUnavailable):
        ehr.context_from_token({"patient": "ePat1"})
    with pytest.raises(ehr.EhrUnavailable):
        ehr.context_from_token("not json")


def test_a_launch_with_no_patient_is_not_an_error():
    """Normal for a launch opened outside a chart."""
    ctx = ehr.context_from_token({"access_token": "at"})
    assert ctx["patient"] is None and ctx["encounter"] is None


# ===========================================================================
# The FHIR client
# ===========================================================================

def test_a_resource_id_cannot_change_which_endpoint_is_called():
    """The id comes from the EHR. A slash or a dot-dot in it must not escape."""
    c = ehr.FhirClient(iss=ISS, token="at", fetch_json=lambda *a, **k: {})
    url = c.url("Patient", "../../metadata")
    assert url == ISS + "/Patient/..%2F..%2Fmetadata"
    assert url.count("/Patient/") == 1


def test_the_client_sends_the_token_as_a_bearer():
    t = _Transport(gets=[PATIENT])
    c = ehr.FhirClient(iss=ISS, token="at-123", fetch_json=t.fetch_json)
    c.read("Patient", "ePat1")
    assert t.get_calls[0]["headers"]["Authorization"] == "Bearer at-123"
    assert t.get_calls[0]["headers"]["Accept"] == "application/fhir+json"


def test_a_failed_read_is_reported_as_the_ehr_being_unavailable():
    """Not as a raw transport error: the caller maps our errors to a status, and
    an unmapped exception would become a 500 with no explanation."""
    t = _Transport(gets=[RuntimeError("connection reset")])
    c = ehr.FhirClient(iss=ISS, token="at", fetch_json=t.fetch_json)
    with pytest.raises(ehr.EhrUnavailable):
        c.read("Patient", "ePat1")


def test_a_read_only_client_refuses_a_write_rather_than_dropping_it():
    """Phase 2 writes a note. A client built without write access must say so,
    not fail quietly and leave a clinician thinking it saved."""
    c = ehr.FhirClient(iss=ISS, token="at", fetch_json=lambda *a, **k: {})
    with pytest.raises(ehr.EhrNotConfigured):
        c.create("DocumentReference", {"status": "current"})


def test_the_write_path_posts_to_the_collection_not_a_resource():
    """Creating is a POST to /DocumentReference, not to /DocumentReference/<id>."""
    seen = {}

    def post_json(url, body, headers=None):
        seen.update({"url": url, "body": body, "headers": headers})
        return {"id": "new-1"}

    c = ehr.FhirClient(iss=ISS, token="at", fetch_json=lambda *a, **k: {},
                       post_json=post_json)
    c.create("DocumentReference", {"status": "current"})
    assert seen["url"] == ISS + "/DocumentReference"
    assert seen["headers"]["Content-Type"] == "application/fhir+json"


# ===========================================================================
# Reducing a resource for display
# ===========================================================================

def test_a_patient_is_reduced_to_what_the_page_shows():
    out = ehr.patient_summary(PATIENT)
    assert out["name"] == "Camila Maria Lopez"
    assert out["birth_date"] == "1980-04-01"
    assert out["id"] == "ePat1"


def test_a_patient_with_a_text_name_uses_it():
    assert ehr.patient_summary({"name": [{"text": "Lopez, Camila"}]})["name"] \
        == "Lopez, Camila"


def test_a_patient_with_no_name_renders_rather_than_crashing():
    """A missing name must not put a stack trace in front of a clinician."""
    for odd in ({}, {"name": []}, {"name": [{}]}, {"name": "nonsense"}, None):
        assert ehr.patient_summary(odd)["name"] is None


def test_an_encounter_is_reduced_to_status_and_start():
    out = ehr.encounter_summary(ENCOUNTER)
    assert out["status"] == "finished"
    assert out["start"] == "2026-09-01T14:00:00Z"
    assert ehr.encounter_summary({"period": "nonsense"})["start"] is None


# ===========================================================================
# The flow, without Flask
# ===========================================================================

def test_the_launch_checks_the_tenant_before_touching_the_network():
    """The ordering IS the security property: an issuer we do not trust must not
    even receive a request from us, because that request carries our client id."""
    t = _Transport(gets=[SMART_DOC])
    with pytest.raises(ehr.EhrRefused):
        ehr.start_launch(iss="https://evil.example/api/FHIR/R4", launch="x",
                         redirect_uri=REDIRECT, scope="launch",
                         tenant_for=_tenant(), fetch_json=t.fetch_json)
    assert t.get_calls == []


def test_a_good_launch_returns_where_to_go_and_what_to_remember():
    t = _Transport(gets=[SMART_DOC])
    out = ehr.start_launch(iss=ISS, launch="lk1", redirect_uri=REDIRECT,
                           scope="launch openid", tenant_for=_tenant(),
                           fetch_json=t.fetch_json)
    assert out["redirect_to"].startswith(AUTHZ)
    assert out["token_url"] == TOKEN
    assert out["iss"] == ISS and out["vendor"] == "epic"
    assert out["state"] and out["verifier"]
    # The challenge on the wire matches the verifier we kept.
    assert _params(out["redirect_to"])["code_challenge"] == \
        ehr.challenge_for(out["verifier"])


def test_a_launch_survives_discovery_being_down():
    t = _Transport(gets=[RuntimeError("connection reset")])
    with pytest.raises(ehr.EhrUnavailable):
        ehr.start_launch(iss=ISS, launch="", redirect_uri=REDIRECT,
                         scope="launch", tenant_for=_tenant(),
                         fetch_json=t.fetch_json)


def test_the_whole_flow_reads_the_patient():
    t = _Transport(gets=[PATIENT, ENCOUNTER], posts=[TOKEN_OK])
    out = ehr.finish_launch(code="c1", state="s", expected_state="s",
                            verifier="v", iss=ISS, token_url=TOKEN,
                            redirect_uri=REDIRECT, tenant_for=_tenant(),
                            fetch_json=t.fetch_json, post_form=t.post_form)
    assert out["patient"]["name"] == "Camila Maria Lopez"
    assert out["encounter"]["status"] == "finished"
    assert out["vendor"] == "epic"
    # And it handed back a client, so phase 2 can write without rebuilding one.
    assert isinstance(out["client"], ehr.FhirClient)


def test_the_flow_refuses_a_state_it_did_not_issue():
    t = _Transport(posts=[TOKEN_OK])
    with pytest.raises(ehr.EhrRefused):
        ehr.finish_launch(code="c1", state="wrong", expected_state="s",
                          verifier="v", iss=ISS, token_url=TOKEN,
                          redirect_uri=REDIRECT, tenant_for=_tenant(),
                          fetch_json=t.fetch_json, post_form=t.post_form)
    assert t.post_calls == []            # never got as far as the exchange


def test_the_flow_refuses_when_a_launch_left_nothing_behind():
    t = _Transport(posts=[TOKEN_OK])
    with pytest.raises(ehr.EhrRefused):
        ehr.finish_launch(code="c1", state="s", expected_state=None,
                          verifier=None, iss=None, token_url=None,
                          redirect_uri=REDIRECT, tenant_for=_tenant(),
                          fetch_json=t.fetch_json, post_form=t.post_form)
    assert t.post_calls == []


def test_the_tenant_is_rechecked_at_the_callback():
    """Cheap, and it means a customer switched off between launch and callback
    stops working immediately rather than at the next launch."""
    t = _Transport(posts=[TOKEN_OK])
    with pytest.raises(ehr.EhrRefused):
        ehr.finish_launch(code="c1", state="s", expected_state="s", verifier="v",
                          iss=ISS, token_url=TOKEN, redirect_uri=REDIRECT,
                          tenant_for=_tenant(allowed=()),   # no longer a customer
                          fetch_json=t.fetch_json, post_form=t.post_form)
    assert t.post_calls == []


def test_the_flow_survives_a_refused_token_exchange():
    t = _Transport(posts=[RuntimeError("400 invalid_grant")])
    with pytest.raises(ehr.EhrUnavailable):
        ehr.finish_launch(code="c1", state="s", expected_state="s", verifier="v",
                          iss=ISS, token_url=TOKEN, redirect_uri=REDIRECT,
                          tenant_for=_tenant(), fetch_json=t.fetch_json,
                          post_form=t.post_form)


def test_the_flow_reads_nothing_when_the_launch_named_no_patient():
    t = _Transport(posts=[{"access_token": "at"}])
    out = ehr.finish_launch(code="c1", state="s", expected_state="s",
                            verifier="v", iss=ISS, token_url=TOKEN,
                            redirect_uri=REDIRECT, tenant_for=_tenant(),
                            fetch_json=t.fetch_json, post_form=t.post_form)
    assert out["patient"]["id"] is None
    assert t.get_calls == []             # nothing to read without an id


# ===========================================================================
# What HTTP owns — the only part that needs Flask
# ===========================================================================

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


class _Enabled:
    """Config patched on for the block."""

    def __init__(self, **over):
        values = {"EHR_ENABLED": True, "EPIC_CLIENT_ID": CLIENT_ID,
                  "EPIC_SANDBOX_CLIENT_SECRET": SECRET,
                  "EHR_ALLOWED_ISS": (ISS,),
                  "EHR_SCOPES": "launch openid fhirUser patient/Patient.read"}
        values.update(over)
        self._patches = [patch.object(config, k, v) for k, v in values.items()]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def test_both_routes_are_invisible_while_the_flag_is_off(client):
    """Nothing about this appears in production until it is switched on."""
    with patch.object(config, "EHR_ENABLED", False):
        assert client.get("/ehr/launch?iss=" + ISS).status_code == 404
        assert client.get("/ehr/callback?code=c&state=s").status_code == 404


def test_each_error_becomes_the_right_status_code(client):
    """The one thing the route owns that nothing else can check. A clinician
    seeing 503 means our configuration; 502 means their EHR; 400 means we
    refused."""
    cases = [(ehr.EhrRefused("no"), 400),
             (ehr.EhrNotConfigured("no"), 503),
             (ehr.EhrUnavailable("no"), 502)]
    for exc, expected in cases:
        with _Enabled(), patch.object(ehr, "start_launch", side_effect=exc):
            rv = client.get("/ehr/launch?iss=" + ISS)
        assert rv.status_code == expected, exc


def test_the_launch_remembers_state_and_the_callback_consumes_it(client):
    """The session round-trip, which is the route's actual job."""
    with _Enabled(), \
         patch.object(routes_ehr, "_fetch_json", return_value=SMART_DOC):
        rv = client.get("/ehr/launch?iss=" + ISS + "&launch=lk1")
    assert rv.status_code == 302
    state = _params(rv.headers["Location"])["state"]

    with _Enabled(), \
         patch.object(routes_ehr, "_post_form", return_value=TOKEN_OK), \
         patch.object(routes_ehr, "_fetch_json", side_effect=[PATIENT, ENCOUNTER]):
        ok = client.get("/ehr/callback?code=c1&state=" + state)
    assert ok.status_code == 200
    assert "Camila Maria Lopez" in ok.get_data(as_text=True)

    # Single use: the same callback again finds nothing to match.
    with _Enabled(), patch.object(routes_ehr, "_post_form") as posted:
        again = client.get("/ehr/callback?code=c1&state=" + state)
    assert again.status_code == 400
    posted.assert_not_called()


def test_the_callback_reports_an_ehr_refusal(client):
    """The EHR can answer with an error instead of a code — a clinician without
    rights, or a cancelled prompt.

    The log line is asserted, not just the 400: a missing code 400s anyway, so
    without this the test would pass with the error branch removed and a failed
    launch in production would be a mystery.
    """
    with _Enabled(), patch.object(app.logger, "warning") as warned:
        rv = client.get("/ehr/callback?error=access_denied&state=x")
    assert rv.status_code == 400
    assert "access_denied" in " ".join(str(c) for c in warned.call_args_list)


def test_the_result_page_is_a_template_not_a_string_in_the_route():
    """Presentation belongs in templates/. It started as a string inside the
    route module and that is exactly the kind of "temporary" that stays."""
    import inspect
    src = inspect.getsource(routes_ehr)
    assert "render_template_string" not in src
    assert "<!doctype" not in src.lower()
    assert os.path.exists(os.path.join(os.path.dirname(__file__), "..",
                                       "templates", "ehr_result.html"))


# ===========================================================================
# Nothing is stored
# ===========================================================================

def _run_a_full_launch(client):
    with _Enabled(), \
         patch.object(routes_ehr, "_fetch_json", return_value=SMART_DOC):
        rv = client.get("/ehr/launch?iss=" + ISS + "&launch=lk1")
    state = _params(rv.headers["Location"])["state"]
    with _Enabled(), \
         patch.object(routes_ehr, "_post_form", return_value=TOKEN_OK), \
         patch.object(routes_ehr, "_fetch_json", side_effect=[PATIENT, ENCOUNTER]):
        client.get("/ehr/callback?code=c1&state=" + state)


def test_no_patient_data_reaches_the_database(client):
    """Whether patient identity belongs in this app is a real decision. A spike
    must not settle it by writing a row."""
    from sqlalchemy import text as _sql
    _run_a_full_launch(client)
    with app.app_context():
        tables = [r[0] for r in db.session.execute(_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")).all()]
        for table in tables:
            blob = repr(db.session.execute(
                _sql("SELECT * FROM %s" % table)).mappings().all())
            assert "Camila" not in blob, table
            assert "1980-04-01" not in blob, table
            assert "ePat1" not in blob, table
            assert "at-123" not in blob, table       # nor the access token


def test_the_audit_log_records_the_launch_without_any_pii(client):
    """Launches are worth recording. Names and dates of birth are not."""
    _run_a_full_launch(client)
    with app.app_context():
        rows = AuditLog.query.filter(AuditLog.event_type.in_(
            ("ehr_launch_started", "ehr_launch_completed"))).all()
        assert len(rows) == 2
        for row in rows:
            for leak in ("Camila", "ePat1", "1980", "at-123"):
                assert leak not in (row.details or ""), leak
        done = [r for r in rows if r.event_type == "ehr_launch_completed"][0]
        assert "epic" in done.details              # vendor, which is not PII


# ===========================================================================
# Wiring
# ===========================================================================

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
    src = open(os.path.join(os.path.dirname(__file__), "..", "config.py"),
               encoding="utf-8").read()
    assert 'os.environ.get("EHR_ENABLED", "false")' in src


def test_a_vendor_is_named_for_wording_only():
    assert ehr.vendor_for_iss(ISS) == "epic"
    assert ehr.vendor_for_iss("https://fhir-ehr.cerner.com/r4") == "oracle"
    assert ehr.vendor_for_iss("https://something.else/api") == ""
    assert ehr.vendor_label("epic") == "Epic"
    assert ehr.vendor_label("nonsense") == "EHR"
