"""
tests/test_ehr_write.py
-----------------------
Writing a session note back into the chart — phase 2.

No network anywhere, same as the launch suite: the write transport is a plain
function passed in, so these tests assert OUR bytes and OUR refusals rather than
Epic's uptime.

This half gets harsher treatment than the read half, because the failure modes
are not symmetrical. A read that goes wrong shows a clinician a blank field. A
write that goes wrong puts a permanent entry in someone's medical record, and
undoing it is work for the health system's staff. So the things under the
microscope here are:

  * THE EXACT BODY. `document_reference_body` is pure, so the precise thing we
    would send is assertable without a token. It is a medical record entry; its
    shape is not something to discover in production.
  * DOUBLE WRITES. A FHIR create answers 201 with a Location header and an
    ALLOWED-TO-BE-EMPTY body. Treating that as a failure makes a clinician press
    the button twice and puts two progress notes in the chart. That path is
    tested directly.
  * REFUSING BEATS GUESSING. Empty note, bad ids, dead token, no launch context
    — each must refuse and say so, and must not reach the transport at all.
  * WHAT GETS STORED. FHIR ids only. A test asserts the name and date of birth
    are not in the row, because that decision is the one most likely to be
    quietly undone by a later change.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-ehr-write")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

import config
import ehr
import TogetherMindsAI as _tm
from TogetherMindsAI import app
import routes_ehr
from models import (db, init_encryption, EhrLaunchContext, AuditLog,
                     SessionSummary, TherapySession, friendly_name_key)

init_encryption(TEST_KEY)

ISS = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
PATIENT = "eOPSKZbz6YIgoilQprzPy0Q3"
ENCOUNTER = "eVffiE7SavpOc0PtATuBQWg3"
PRACTITIONER = ISS + "/Practitioner/ePractitionerIdHere"
NOW = datetime(2026, 9, 14, 3, 30, 0, tzinfo=timezone.utc)


def _decoded(body):
    """The note text back out of the attachment, so a test reads what a
    clinician would rather than a base64 blob."""
    data = body["content"][0]["attachment"]["data"]
    return base64.b64decode(data).decode("utf-8")


# ===========================================================================
# The body we would send
# ===========================================================================

def test_the_body_is_a_document_reference_against_this_patient_and_encounter():
    """The whole point of keeping this function pure: the exact medical record
    entry is assertable without a token or a network."""
    body = ehr.document_reference_body(
        note_text="Session recap.", patient_id=PATIENT,
        encounter_id=ENCOUNTER, author=PRACTITIONER, now=NOW)

    assert body["resourceType"] == "DocumentReference"
    assert body["status"] == "current"
    assert body["subject"] == {"reference": "Patient/" + PATIENT}
    assert body["context"]["encounter"] == [
        {"reference": "Encounter/" + ENCOUNTER}]
    assert body["author"] == [{"reference": PRACTITIONER}]
    assert body["content"][0]["attachment"]["contentType"] == "text/plain"
    assert _decoded(body) == "Session recap."


def test_the_note_is_filed_as_final_not_preliminary():
    """A clinician read the text and pressed the button. Filing it as
    preliminary would misdescribe what happened."""
    body = ehr.document_reference_body(note_text="x", patient_id=PATIENT)
    assert body["docStatus"] == "final"


def test_the_document_type_is_a_loinc_progress_note_and_is_overridable():
    """Default is a progress note, because that is what a session recap is. It
    is overridable because every health system maps document types in its own
    build, and an unmapped code is refused at write time."""
    body = ehr.document_reference_body(note_text="x", patient_id=PATIENT)
    coding = body["type"]["coding"][0]
    assert coding["system"] == "http://loinc.org"
    assert coding["code"] == "11506-3"

    custom = ehr.document_reference_body(
        note_text="x", patient_id=PATIENT,
        type_code="34109-9", type_display="Note")
    assert custom["type"]["coding"][0]["code"] == "34109-9"
    assert custom["type"]["text"] == "Note"


def test_the_date_is_omitted_rather_than_invented_when_no_clock_is_given():
    """The function takes a clock instead of reading one, so a test can assert
    the timestamp. With no clock it says nothing rather than guessing."""
    assert "date" not in ehr.document_reference_body(
        note_text="x", patient_id=PATIENT)
    dated = ehr.document_reference_body(
        note_text="x", patient_id=PATIENT, now=NOW)
    assert dated["date"] == "2026-09-14T03:30:00Z"


def test_a_launch_with_no_encounter_files_a_note_without_one():
    """Epic can launch with a patient and no encounter. That is a normal note,
    not an error — but it must not carry an empty context block."""
    body = ehr.document_reference_body(note_text="x", patient_id=PATIENT)
    assert "context" not in body
    assert "author" not in body


def test_non_ascii_survives_the_trip():
    """Clinical prose has names, accents and quote marks in it. base64 of UTF-8
    bytes, not of whatever the default encoding happens to be."""
    text = "Patient prefers “Zoë”. Affect: café-calm — settled."
    body = ehr.document_reference_body(note_text=text, patient_id=PATIENT)
    assert _decoded(body) == text


# ===========================================================================
# Refusing beats guessing
# ===========================================================================

def test_an_empty_note_is_refused():
    """A blank progress note is worse than no note: it reads as a clinician
    having documented the session and found nothing worth saying."""
    for blank in ("", "   ", "\n\t ", None):
        with pytest.raises(ehr.EhrRefused):
            ehr.document_reference_body(note_text=blank, patient_id=PATIENT)


def test_a_note_with_no_patient_is_refused():
    """Nothing to address it to. A DocumentReference with no subject would
    either be rejected or, worse, land somewhere unintended."""
    for bad in ("", None, "   "):
        with pytest.raises(ehr.EhrRefused):
            ehr.document_reference_body(note_text="x", patient_id=bad)


def test_ids_that_would_change_the_reference_are_refused():
    """These ids come from the EHR and are then pasted into a reference string.
    An id carrying a slash would silently change which resource we claim to be
    writing against, so the FHIR id rule is enforced rather than assumed."""
    for bad in ("abc/def", "../Patient/other", "a b", "x" * 65, "a?b", "a#b"):
        with pytest.raises(ehr.EhrRefused):
            ehr.document_reference_body(note_text="x", patient_id=bad)
        with pytest.raises(ehr.EhrRefused):
            ehr.document_reference_body(note_text="x", patient_id=PATIENT,
                                        encounter_id=bad)


def test_the_fhir_id_rule_accepts_what_epic_actually_sends():
    """Guards that refuse real input are worse than no guard. These are the two
    ids the sandbox returned on 2026-09-13."""
    assert ehr.valid_fhir_id(PATIENT)
    assert ehr.valid_fhir_id(ENCOUNTER)
    assert ehr.valid_fhir_id("a.b-c123")


# ===========================================================================
# The token has to outlive the session
# ===========================================================================

def test_the_expiry_is_absolute_because_it_gets_stored_and_read_back_later():
    """A duration would silently mean "from whenever you happen to read this".
    The write happens in a different request from the launch."""
    assert ehr.token_expiry(3600, NOW) == NOW + timedelta(seconds=3600)


def test_an_unstated_or_nonsense_expiry_falls_back_short_not_long():
    """Guessing LONG is the dangerous direction: it produces a write attempt
    with a token the server already dropped. Guessing short just asks the
    clinician to launch again."""
    for junk in (None, "", "soon", 0, -5, {}):
        assert ehr.token_expiry(junk, NOW) == NOW + timedelta(
            seconds=ehr.FALLBACK_TOKEN_SECONDS)


def test_a_token_is_unusable_inside_the_margin():
    """A chart write is a round trip plus Epic's own processing. A token that
    dies mid-write is the worst case, because a refusal cannot be told apart
    from a note that landed."""
    assert ehr.token_usable(NOW + timedelta(minutes=30), NOW)
    assert not ehr.token_usable(NOW + timedelta(seconds=30), NOW)
    assert not ehr.token_usable(NOW - timedelta(seconds=1), NOW)


def test_a_missing_expiry_counts_as_unusable():
    """An unknown expiry on the credential we are about to write a medical
    record with is not a thing to be optimistic about."""
    assert not ehr.token_usable(None, NOW)


def test_a_naive_timestamp_from_the_database_is_read_as_utc():
    """SQLite and Postgres both hand back naive datetimes for a DateTime
    column. Comparing one against an aware `now` raises TypeError, which would
    surface as a 500 on a working launch."""
    naive = (NOW + timedelta(minutes=30)).replace(tzinfo=None)
    assert ehr.token_usable(naive, NOW)


# ===========================================================================
# 201 with an empty body is a SUCCESS
# ===========================================================================

def test_a_created_note_is_recognised_from_the_location_header():
    """Epic answers a create with 201, a Location header, and usually no body.
    Reading that as a failure is what puts two progress notes in a chart."""
    ref = ehr.created_reference(
        {"_status": 201, "_location": ISS + "/DocumentReference/abc123"})
    assert ref.endswith("/DocumentReference/abc123")


def test_a_created_note_is_recognised_from_a_returned_resource():
    """Some servers do echo the resource back. Both shapes count."""
    assert ehr.created_reference(
        {"resourceType": "DocumentReference", "id": "abc123"}
    ) == "DocumentReference/abc123"


def test_an_unrecognisable_response_yields_no_reference_but_is_not_an_error():
    """We could not tell WHERE it went. That is not the same as it not having
    gone, so this returns "" and the caller still treats the write as done."""
    assert ehr.created_reference({}) == ""
    assert ehr.created_reference(None) == ""
    assert ehr.created_reference("201 Created") == ""


# ===========================================================================
# write_note over a fake transport
# ===========================================================================

class _Writer:
    """A stand-in FHIR server that records what it was sent."""

    def __init__(self, response=None, blow_up=False):
        self.response = response if response is not None else {
            "_status": 201, "_location": ISS + "/DocumentReference/new1"}
        self.blow_up = blow_up
        self.posts = []

    def post_json(self, url, body, headers=None):
        self.posts.append({"url": url, "body": body, "headers": headers or {}})
        if self.blow_up:
            raise RuntimeError("epic said no")
        return self.response

    def fetch_json(self, url, headers=None):       # never used by a write
        raise AssertionError("a write must not read")


def _client(writer, token="tok-abc"):
    return ehr.FhirClient(iss=ISS, token=token,
                          fetch_json=writer.fetch_json,
                          post_json=writer.post_json)


def test_write_note_posts_to_the_document_reference_endpoint_with_the_token():
    w = _Writer()
    out = ehr.write_note(client=_client(w), note_text="Recap.",
                         patient_id=PATIENT, encounter_id=ENCOUNTER, now=NOW)

    assert len(w.posts) == 1
    sent = w.posts[0]
    assert sent["url"] == ISS + "/DocumentReference"
    assert sent["headers"]["Authorization"] == "Bearer tok-abc"
    assert sent["headers"]["Content-Type"] == "application/fhir+json"
    assert _decoded(sent["body"]) == "Recap."
    assert out["reference"].endswith("/DocumentReference/new1")


def test_a_read_only_client_refuses_the_write_rather_than_dropping_it():
    """FhirClient built without a write transport is the phase 1 client. It must
    say so, because a silently dropped chart write is the worst outcome here."""
    read_only = ehr.FhirClient(iss=ISS, token="t", fetch_json=lambda *a, **k: {})
    with pytest.raises(ehr.EhrNotConfigured):
        ehr.write_note(client=read_only, note_text="x", patient_id=PATIENT)


def test_a_transport_failure_becomes_ehr_unavailable_not_a_raw_exception():
    """So the route can turn it into something a clinician can act on, instead
    of a 500."""
    w = _Writer(blow_up=True)
    with pytest.raises(ehr.EhrUnavailable):
        ehr.write_note(client=_client(w), note_text="x", patient_id=PATIENT)


def test_a_refused_body_never_reaches_the_transport():
    """The refusal has to happen BEFORE the network call, or an empty note has
    already been sent by the time we object."""
    w = _Writer()
    with pytest.raises(ehr.EhrRefused):
        ehr.write_note(client=_client(w), note_text="  ", patient_id=PATIENT)
    assert w.posts == []


# ===========================================================================
# What HTTP owns
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


class _WriteOn:
    """Both switches on, which is what a write needs."""

    def __init__(self, **over):
        values = {"EHR_ENABLED": True, "EHR_WRITE_ENABLED": True,
                  "EHR_ALLOWED_ISS": (ISS,),
                  "EHR_NOTE_TYPE_CODE": "11506-3",
                  "EHR_NOTE_TYPE_DISPLAY": "Progress note"}
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


def _context_row(expires_in_minutes=45, **over):
    """A launch context as the callback would have left it."""
    now = datetime.now(timezone.utc)
    values = dict(
        launch_id="launch-1", iss=ISS, patient_fhir_id=PATIENT,
        encounter_fhir_id=ENCOUNTER, fhir_user=PRACTITIONER,
        access_token="tok-abc",
        token_expires_at=now + timedelta(minutes=expires_in_minutes),
        created_at=now)
    values.update(over)
    row = EhrLaunchContext(**values)
    db.session.add(row)
    db.session.commit()
    return row


def _hold(client, launch_id="launch-1"):
    with client.session_transaction() as s:
        s["_ehr_launch_id"] = launch_id


def test_writing_is_invisible_while_its_own_switch_is_off(client):
    """Two switches, not one. A launch can be on for testing without the chart
    being writable."""
    with _WriteOn(EHR_WRITE_ENABLED=False):
        assert client.post("/ehr/write-note").status_code == 404
    with _WriteOn(EHR_ENABLED=False):
        assert client.post("/ehr/write-note").status_code == 404


def test_the_write_route_is_not_exempt_from_csrf():
    """It changes state in someone else's system of record. The exemption list
    is for endpoints authenticated by signature, which this is not."""
    assert "ehr_write_note" not in _tm._CSRF_EXEMPT


def test_the_load_summary_route_is_not_exempt_from_csrf():
    assert "ehr_load_summary" not in _tm._CSRF_EXEMPT


def test_a_press_with_no_open_launch_says_so_and_writes_nothing(client):
    """The session expired or the cookie is from another launch. Stay on the
    page and say what to do, rather than 500 or redirect somewhere."""
    with _WriteOn(), patch.object(routes_ehr, "_post_json") as post:
        rv = client.post("/ehr/write-note", data={"note_text": "Recap."})
    assert rv.status_code == 200
    assert b"no longer open" in rv.data
    post.assert_not_called()


def test_the_happy_path_files_the_note_and_confirms_before_moving(client):
    """Confirms success on the same page. A clinician who is not told whether
    the note landed will press the button again."""
    with _WriteOn():
        _context_row()
        _hold(client)
        w = _Writer()
        with patch.object(routes_ehr, "_post_json", w.post_json):
            rv = client.post("/ehr/write-note",
                             data={"note_text": "Session recap."})

        assert rv.status_code == 200
        assert b"Filed in the chart" in rv.data
        assert len(w.posts) == 1
        assert _decoded(w.posts[0]["body"]) == "Session recap."

        row = db.session.get(EhrLaunchContext, "launch-1")
        assert row.written_at is not None
        assert "DocumentReference/new1" in row.written_reference
        # The token has done its only job. A written note must not leave a live
        # credential sitting in the table.
        assert row.access_token == ""


def test_the_note_carries_the_configured_document_type(client):
    """The code is configurable per deployment because each health system maps
    document types in its own build. Prove the config is actually used."""
    with _WriteOn(EHR_NOTE_TYPE_CODE="34109-9", EHR_NOTE_TYPE_DISPLAY="Note"):
        _context_row()
        _hold(client)
        w = _Writer()
        with patch.object(routes_ehr, "_post_json", w.post_json):
            client.post("/ehr/write-note", data={"note_text": "x"})
    coding = w.posts[0]["body"]["type"]["coding"][0]
    assert coding["code"] == "34109-9"
    assert coding["display"] == "Note"


def test_a_second_press_says_already_filed_and_does_not_duplicate(client):
    """The row is kept after success precisely so this can be answered. Two
    progress notes for one session is the failure this prevents."""
    with _WriteOn():
        _context_row()
        _hold(client)
        w = _Writer()
        with patch.object(routes_ehr, "_post_json", w.post_json):
            client.post("/ehr/write-note", data={"note_text": "Recap."})
            rv = client.post("/ehr/write-note", data={"note_text": "Recap."})

    assert rv.status_code == 200
    assert b"Filed in the chart" in rv.data
    assert len(w.posts) == 1        # still one


def test_an_expired_token_refuses_without_reaching_epic(client):
    """There is no refresh token — persistent access is off on the Epic
    registration — so this is unrecoverable here. Say so plainly rather than
    sending a dead bearer token at a chart."""
    with _WriteOn():
        _context_row(expires_in_minutes=-5)
        _hold(client)
        with patch.object(routes_ehr, "_post_json") as post:
            rv = client.post("/ehr/write-note", data={"note_text": "Recap."})

    assert rv.status_code == 200
    assert b"timed out" in rv.data
    assert b"nothing was written" in rv.data
    post.assert_not_called()
    assert db.session.get(EhrLaunchContext, "launch-1").written_at is None


def test_a_refusal_from_epic_keeps_the_text_and_stays_on_the_page(client):
    """On failure: stay, show the error, keep what they wrote. Navigating away
    would hide the bug and lose the note."""
    with _WriteOn():
        _context_row()
        _hold(client)
        w = _Writer(blow_up=True)
        with patch.object(routes_ehr, "_post_json", w.post_json):
            rv = client.post("/ehr/write-note",
                             data={"note_text": "Careful wording here."})

        assert rv.status_code == 200
        assert b"did not accept the note" in rv.data
        assert b"Careful wording here." in rv.data       # not lost
        row = db.session.get(EhrLaunchContext, "launch-1")
        assert row.written_at is None                    # retryable
        assert row.access_token == "tok-abc"


def test_an_empty_form_is_refused_before_the_transport(client):
    with _WriteOn():
        _context_row()
        _hold(client)
        with patch.object(routes_ehr, "_post_json") as post:
            rv = client.post("/ehr/write-note", data={"note_text": "   "})
    assert b"nothing written to file" in rv.data
    post.assert_not_called()


def test_an_absurdly_long_note_is_refused(client):
    """A progress note is prose. Past the limit it is a paste accident or
    someone probing, and either way the chart should not receive it."""
    with _WriteOn():
        _context_row()
        _hold(client)
        with patch.object(routes_ehr, "_post_json") as post:
            rv = client.post("/ehr/write-note",
                             data={"note_text": "x" * (routes_ehr.MAX_NOTE_CHARS + 1)})
    assert b"too long to file" in rv.data
    post.assert_not_called()


# ===========================================================================
# What gets stored — the decision most likely to be quietly undone
# ===========================================================================

def test_the_stored_row_holds_fhir_ids_and_no_patient_identity(client):
    """FHIR ids only, on purpose. Keeping the name and date of birth would turn
    this database into a patient index, which is a different product with
    different obligations."""
    columns = set(EhrLaunchContext.__table__.columns.keys())
    for banned in ("name", "patient_name", "birth_date", "birthdate", "dob",
                   "gender"):
        assert banned not in columns
    assert {"patient_fhir_id", "encounter_fhir_id"} <= columns


def test_the_token_and_ids_are_encrypted_at_rest(client):
    """The row holds a live bearer token to a FHIR server full of patient data.
    Read the RAW bytes back rather than the ORM's decrypted view, because a
    string comparison through the ORM would pass either way."""
    with _WriteOn():
        _context_row()
        raw = db.session.execute(db.text(
            "SELECT access_token, patient_fhir_id FROM ehr_launch_contexts"
        )).first()

    assert raw[0] != "tok-abc"
    assert "tok-abc" not in raw[0]
    assert raw[1] != PATIENT
    assert PATIENT not in raw[1]


def test_the_audit_log_records_that_a_note_was_filed_and_not_what_it_said(client):
    """Metadata only. That rule does not bend for a new integration, and a note
    is the most sensitive text in the app."""
    secret_text = "Client disclosed something highly identifying."
    with _WriteOn():
        _context_row()
        _hold(client)
        w = _Writer()
        with patch.object(routes_ehr, "_post_json", w.post_json):
            client.post("/ehr/write-note", data={"note_text": secret_text})

        rows = AuditLog.query.filter_by(event_type="ehr_note_written").all()
        assert len(rows) == 1
        blob = json.dumps([
            {c.name: str(getattr(r, c.name)) for c in AuditLog.__table__.columns}
            for r in rows])

    assert secret_text not in blob
    assert PATIENT not in blob
    assert "highly identifying" not in blob


def test_expired_contexts_are_swept(client):
    """A dead credential sitting in a table is only a liability. Swept on the
    way through a launch, because that is the only moment this table grows."""
    with _WriteOn():
        _context_row(launch_id="dead", expires_in_minutes=-30)
        _context_row(launch_id="alive", expires_in_minutes=30)
        routes_ehr._sweep_expired_contexts(datetime.now(timezone.utc))

        assert db.session.get(EhrLaunchContext, "dead") is None
        assert db.session.get(EhrLaunchContext, "alive") is not None


# ===========================================================================
# Loading a session recap ("Load recap") into the note box
# ===========================================================================

TMAI_SESSION_ID = "AAAABBBBCCCCDDDD"   # SESSION_ID_LENGTH characters


def _session_row(session_id=TMAI_SESSION_ID, friendly_name=None):
    now = datetime.now(timezone.utc)
    ts = TherapySession(
        id=session_id, mode="solo", created_by="clinician-1", created_at=now,
        friendly_name=friendly_name,
        friendly_name_key=friendly_name_key(friendly_name) if friendly_name else None,
    )
    db.session.add(ts)
    db.session.commit()
    return ts


def _summary_row(session_id=TMAI_SESSION_ID, clinical="Client recap here.",
                  codes_rationale="F41.1 fits the anxiety discussed."):
    payload = {"session_id": session_id, "clinical": clinical,
               "codes_rationale": codes_rationale, "codes": [],
               "client_recap": "", "disclaimer": "", "narrative_available": True,
               "cached": False}
    row = SessionSummary(session_id=session_id, payload=json.dumps(payload),
                         message_count=4)
    db.session.add(row)
    db.session.commit()
    return row


def test_loading_a_recap_is_invisible_while_writing_is_off(client):
    with _WriteOn(EHR_WRITE_ENABLED=False):
        assert client.post("/ehr/load-summary").status_code == 404


def test_loading_a_recap_fills_the_note_with_clinical_and_coding_notes(client):
    with _WriteOn():
        _context_row()
        _session_row()
        _summary_row()
        _hold(client)
        rv = client.post("/ehr/load-summary",
                         data={"tmai_session": TMAI_SESSION_ID})

    assert b"Client recap here." in rv.data
    assert b"Coding considerations:" in rv.data
    assert b"F41.1 fits the anxiety discussed." in rv.data
    assert b"nothing has been sent to the chart" in rv.data

    ctx = db.session.get(EhrLaunchContext, "launch-1")
    assert ctx.session_id == TMAI_SESSION_ID


def test_loading_a_recap_by_friendly_name(client):
    with _WriteOn():
        _context_row()
        _session_row(friendly_name="Smith weekly")
        _summary_row()
        _hold(client)
        rv = client.post("/ehr/load-summary",
                         data={"tmai_session": "Smith weekly"})

    assert b"Client recap here." in rv.data


def test_loading_a_recap_never_reaches_epic(client):
    """Loading fills a textarea. It must not be indistinguishable from filing
    — the only thing that may ever POST to Epic is "File this note"."""
    with _WriteOn():
        _context_row()
        _session_row()
        _summary_row()
        _hold(client)
        with patch.object(routes_ehr, "_post_json") as post:
            client.post("/ehr/load-summary",
                       data={"tmai_session": TMAI_SESSION_ID})
    post.assert_not_called()


def test_loading_a_recap_for_an_unknown_session_refuses(client):
    with _WriteOn():
        _context_row()
        _hold(client)
        rv = client.post("/ehr/load-summary",
                         data={"tmai_session": "no-such-session"})
    assert b"No TogetherMindsAI session found" in rv.data


def test_loading_a_recap_with_no_cached_summary_refuses(client):
    with _WriteOn():
        _context_row()
        _session_row()
        _hold(client)
        rv = client.post("/ehr/load-summary",
                         data={"tmai_session": TMAI_SESSION_ID})
    assert b"No summary is cached" in rv.data


def test_the_linked_session_id_is_encrypted_at_rest(client):
    """Same rule as the FHIR ids on this row: read the RAW bytes, not the
    ORM's decrypted view."""
    with _WriteOn():
        _context_row()
        _session_row()
        _summary_row()
        _hold(client)
        client.post("/ehr/load-summary", data={"tmai_session": TMAI_SESSION_ID})

        raw = db.session.execute(db.text(
            "SELECT session_id FROM ehr_launch_contexts"
        )).first()

    assert raw[0] != TMAI_SESSION_ID
    assert TMAI_SESSION_ID not in raw[0]
