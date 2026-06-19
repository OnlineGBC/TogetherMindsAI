"""
tests/test_session_summary.py
-----------------------------
Therapist-only session summary:

  - clinical_summary.generate: JSON parse, graceful failure, silence on empty
  - _surfaced_codes: grounded reference codes, deduped
  - GET /session/<id>/summary: therapist-only (403 for everyone else)
  - transcript download: therapist copy carries the summary, a client copy never does
  - summary-generation failure still yields a valid download with the codes
"""

import os
import sys
import io
import json
import uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-summary")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import clinical_summary
from TogetherMindsAI import app, _surfaced_codes, _session_summary_payload
from models import db, init_encryption, TherapySession, ChatMessage, CopilotCard
from session_id import generate_session_id

init_encryption(TEST_KEY)


@pytest.fixture
def enc_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db._app_engines[app] = {None: engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def _claude_returning(text):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    cl = MagicMock()
    cl.messages.create.return_value = msg
    return cl


def _ref_payload(code, source="ICD-10-CM (CMS)"):
    return json.dumps({"type": "reference", "code": code, "source": source})


def _seed_therapist_session(therapist_id, client_id):
    """A therapist-led solo session with a therapist turn, a client turn, and two
    reference cards (one a duplicate code)."""
    sid = generate_session_id()
    now = datetime.now(timezone.utc)
    db.session.add(TherapySession(
        id=sid, mode="solo", created_by=therapist_id, created_at=now,
        retention_expires_at=now + timedelta(days=30), therapist_id=therapist_id,
    ))
    db.session.add(ChatMessage(session_id=sid, user_id=therapist_id,
                               display_name="Solo1", text="What brings you in?"))
    db.session.add(ChatMessage(session_id=sid, user_id=client_id,
                               display_name="Solo2", text="I lost my job and can't pay rent"))
    db.session.add(CopilotCard(
        session_id=sid, card_type="reference",
        text="The conversation touches on features associated with Adjustment disorder. Offered...",
        payload=_ref_payload("ICD-10 F43.2 · ICD-11 6B43"), confidence=0.5))
    db.session.add(CopilotCard(
        session_id=sid, card_type="reference",
        text="The conversation touches on features associated with Adjustment disorder. Offered...",
        payload=_ref_payload("ICD-10 F43.2 · ICD-11 6B43"), confidence=0.5))   # duplicate code
    db.session.add(CopilotCard(
        session_id=sid, card_type="reference",
        text="The conversation touches on features associated with Generalized anxiety disorder. Offered...",
        payload=_ref_payload("ICD-10 F41.1 · ICD-11 6B00"), confidence=0.5))
    db.session.commit()
    return sid


def _docx_text(resp):
    from docx import Document
    doc = Document(io.BytesIO(resp.data))
    return "\n".join(p.text for p in doc.paragraphs)


# ---------------------------------------------------------------------------
# clinical_summary.generate
# ---------------------------------------------------------------------------

def test_generate_parses_three_parts():
    raw = json.dumps({"clinical": "Detailed recap.", "codes_rationale": "F43.2 fits.",
                      "client_recap": "Today we talked about work stress."})
    with patch("clinical_summary._get_claude_client", return_value=_claude_returning(raw)):
        out = clinical_summary.generate("Therapist: hi\nClient: stressed", [], mode="solo")
    assert out["clinical"] == "Detailed recap."
    assert out["codes_rationale"] == "F43.2 fits."
    assert out["client_recap"].startswith("Today we talked")


def test_generate_none_on_bad_json():
    with patch("clinical_summary._get_claude_client", return_value=_claude_returning("not json")):
        assert clinical_summary.generate("Client: hi", []) is None


def test_generate_none_on_api_error():
    bad = MagicMock()
    bad.messages.create.side_effect = RuntimeError("API down")
    with patch("clinical_summary._get_claude_client", return_value=bad):
        assert clinical_summary.generate("Client: hi", []) is None


def test_generate_empty_transcript_makes_no_call():
    assert clinical_summary.generate("   ", []) is None


# ---------------------------------------------------------------------------
# _surfaced_codes
# ---------------------------------------------------------------------------

def test_surfaced_codes_deduped_and_grounded(enc_client):
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
        codes = _surfaced_codes(sid)
    seen = [c["code"] for c in codes]
    assert seen == ["ICD-10 F43.2 · ICD-11 6B43", "ICD-10 F41.1 · ICD-11 6B00"]  # dedup, in order
    assert codes[0]["label"] == "Adjustment disorder"                            # parsed from card text


# ---------------------------------------------------------------------------
# GET /session/<id>/summary — therapist only
# ---------------------------------------------------------------------------

def test_summary_endpoint_forbidden_for_non_therapist(enc_client):
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "client-1"            # a client, not the therapist
    assert enc_client.get(f"/session/{sid}/summary").status_code == 403


def test_summary_endpoint_returns_payload_for_therapist(enc_client):
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    fake = {"clinical": "Recap.", "codes_rationale": "F43.2 fits.", "client_recap": "We talked."}
    with patch("clinical_summary.generate", return_value=fake):
        rv = enc_client.get(f"/session/{sid}/summary")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["clinical"] == "Recap."
    assert [c["code"] for c in data["codes"]] == ["ICD-10 F43.2 · ICD-11 6B43", "ICD-10 F41.1 · ICD-11 6B00"]
    assert "decision support" in data["disclaimer"].lower()


# ---------------------------------------------------------------------------
# Transcript download — therapist gets the summary, client never does
# ---------------------------------------------------------------------------

def test_therapist_docx_contains_summary(enc_client):
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    fake = {"clinical": "CLINICAL_RECAP_MARKER.", "codes_rationale": "F43.2 fits best.",
            "client_recap": "CLIENT_DRAFT_MARKER."}
    with patch("clinical_summary.generate", return_value=fake):
        rv = enc_client.get(f"/transcript/{sid}/docx")
    assert rv.status_code == 200
    text = _docx_text(rv)
    assert "Clinician Summary" in text
    assert "CLINICAL_RECAP_MARKER." in text
    assert "F43.2" in text                       # grounded code rendered
    assert "CLIENT_DRAFT_MARKER." in text        # client draft is in the THERAPIST's copy


def test_client_docx_has_no_summary(enc_client):
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "client-1"                # client posted a message → may download transcript
    fake = {"clinical": "CLINICAL_RECAP_MARKER.", "codes_rationale": "x", "client_recap": "y"}
    with patch("clinical_summary.generate", return_value=fake) as gen:
        rv = enc_client.get(f"/transcript/{sid}/docx")
    assert rv.status_code == 200
    text = _docx_text(rv)
    assert "Clinician Summary" not in text
    assert "CLINICAL_RECAP_MARKER." not in text
    gen.assert_not_called()                      # never even generated for a client


def test_therapist_docx_still_works_when_narrative_fails(enc_client):
    """If summary generation fails, the download still succeeds with the grounded
    codes and a clear note — a download must never break."""
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    with patch("clinical_summary.generate", return_value=None):
        rv = enc_client.get(f"/transcript/{sid}/docx")
    assert rv.status_code == 200
    text = _docx_text(rv)
    assert "Clinician Summary" in text
    assert "F43.2" in text                       # codes still present
    assert "narrative unavailable" in text.lower()
