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
from TogetherMindsAI import app, _surfaced_codes, _session_summary_payload, _transcript_data
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

def test_generate_returns_three_plain_text_fields():
    """Each field is its own plain-text call (no JSON). With codes present, all
    three fields are produced — one call each."""
    cl = _claude_returning("A plain-text field.")
    codes = [{"label": "Adjustment disorder", "code": "F43.2", "source": "ICD-10-CM"}]
    with patch("clinical_summary._get_claude_client", return_value=cl):
        out = clinical_summary.generate("Therapist: hi\nClient: stressed", codes, mode="solo")
    assert out["clinical"] == "A plain-text field."
    assert out["codes_rationale"] == "A plain-text field."
    assert out["client_recap"] == "A plain-text field."
    assert cl.messages.create.call_count == 3          # one call per field


def test_generate_skips_codes_call_when_no_codes():
    """With no surfaced codes, the codes-rationale call is skipped entirely."""
    cl = _claude_returning("text")
    with patch("clinical_summary._get_claude_client", return_value=cl):
        out = clinical_summary.generate("Client: hi", [])
    assert out["codes_rationale"] == ""
    assert cl.messages.create.call_count == 2          # clinical + client_recap only


def test_generate_partial_result_when_one_field_fails():
    """A failure in one field's call must not lose the others — plain text per
    field means partial results instead of all-or-nothing."""
    ok = MagicMock(); ok.content = [MagicMock(text="ok")]; ok.stop_reason = "end_turn"
    cl = MagicMock()
    # order: clinical (fails), codes_rationale (ok), client_recap (ok)
    cl.messages.create.side_effect = [RuntimeError("boom"), ok, ok]
    codes = [{"label": "X", "code": "F1", "source": "s"}]
    with patch("clinical_summary._get_claude_client", return_value=cl):
        out = clinical_summary.generate("Client: hi", codes)
    assert out is not None
    assert out["clinical"] == ""                        # failed field omitted
    assert out["codes_rationale"] == "ok"
    assert out["client_recap"] == "ok"                  # others still produced


def test_generate_empty_transcript_makes_no_call():
    assert clinical_summary.generate("   ", []) is None


def test_call_uses_6000_max_tokens():
    """Per-field output ceiling is 6000 tokens."""
    cl = _claude_returning("x")
    with patch("clinical_summary._get_claude_client", return_value=cl):
        clinical_summary._call("sys", "user", label="clinical")
    _, kwargs = cl.messages.create.call_args
    assert kwargs["max_tokens"] == 6000


def test_call_returns_truncated_text_on_max_tokens():
    """A field that hits the token ceiling still returns its (partial) text —
    truncation is logged, not fatal (and never breaks JSON, since there is none)."""
    msg = MagicMock(); msg.content = [MagicMock(text="partial answer")]; msg.stop_reason = "max_tokens"
    cl = MagicMock(); cl.messages.create.return_value = msg
    with patch("clinical_summary._get_claude_client", return_value=cl):
        out = clinical_summary._call("sys", "user", label="clinical")
    assert out == "partial answer"


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
    assert "Clinician summary" in text
    assert "CLINICAL_RECAP_MARKER." in text
    assert "F43.2" in text                       # grounded code rendered
    assert "CLIENT_DRAFT_MARKER." in text        # client draft is in the THERAPIST's copy


def test_client_cannot_download_clinician_led_session(enc_client):
    """A client may not download a clinician-led session's record at all: the
    transcript routes return 403, and the clinician summary is never generated."""
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "client-1"                # a client — never downloads a clinician-led record
    fake = {"clinical": "CLINICAL_RECAP_MARKER.", "codes_rationale": "x", "client_recap": "y"}
    with patch("clinical_summary.generate", return_value=fake) as gen:
        rv = enc_client.get(f"/transcript/{sid}/docx")
    assert rv.status_code == 403
    gen.assert_not_called()                      # never even generated for a client


def test_summary_is_cached_across_calls(enc_client):
    """The slow LLM call runs once; a second request with the same conversation
    is served from the cache (so repeat downloads/console views are instant)."""
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
        ts = db.session.get(TherapySession, sid)
        gen = MagicMock(return_value={"clinical": "c", "codes_rationale": "r", "client_recap": "cr"})
        with patch("clinical_summary.generate", gen):
            first = _session_summary_payload(sid, ts)
            second = _session_summary_payload(sid, ts)
    assert gen.call_count == 1            # second call served from cache
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["clinical"] == "c"


def test_failed_summary_is_not_cached(enc_client):
    """A failed narrative (empty clinical) must NOT be cached, so the next
    download retries instead of being frozen 'unavailable' forever."""
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
        ts = db.session.get(TherapySession, sid)
        gen = MagicMock(return_value={"clinical": "", "codes_rationale": "", "client_recap": ""})
        with patch("clinical_summary.generate", gen):
            first = _session_summary_payload(sid, ts)
            second = _session_summary_payload(sid, ts)
    assert gen.call_count == 2            # not cached → retried
    assert first["narrative_available"] is False
    assert second["cached"] is False      # never served from cache


def test_summary_cache_invalidated_by_new_message(enc_client):
    """A new message bumps the covered count, so the summary regenerates (never stale)."""
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
        ts = db.session.get(TherapySession, sid)
        gen = MagicMock(return_value={"clinical": "c", "codes_rationale": "r", "client_recap": "cr"})
        with patch("clinical_summary.generate", gen):
            _session_summary_payload(sid, ts)
            db.session.add(ChatMessage(session_id=sid, user_id="client-1", text="a new turn"))
            db.session.commit()
            _session_summary_payload(sid, ts)
    assert gen.call_count == 2            # conversation changed → regenerated


def test_therapist_pdf_renders_with_summary(enc_client):
    """Regression: the therapist PDF must render with a multi-paragraph summary —
    chained multi_cell calls previously crashed fpdf ('not enough horizontal space')."""
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    fake = {
        "clinical": "Para one — racing heart when bills arrive.\n\nPara two — nightmares since job loss.",
        "codes_rationale": "F43.2 (Adjustment disorder) fits the stressor-linked onset.",
        "client_recap": "Today we talked about the stress of the job loss and some next steps.",
    }
    with patch("clinical_summary.generate", return_value=fake):
        rv = enc_client.get(f"/transcript/{sid}/pdf")
    assert rv.status_code == 200
    assert rv.data[:4] == b"%PDF"
    assert rv.mimetype == "application/pdf"


def test_transcript_mode_uses_session_not_user(enc_client):
    """Therapist-led sessions have a Clinician creator (no User row), so mode must
    come from the session itself — not resolve to 'Unknown'."""
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
        _messages, mode, _generated = _transcript_data(sid)
    assert mode == "Solo"


def test_transcript_download_is_audited(enc_client):
    """A transcript download is a PHI disclosure and must be logged."""
    from models import AuditLog
    with app.app_context():
        sid = _seed_therapist_session("ther-1", "client-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    with patch("clinical_summary.generate", return_value=None):
        rv = enc_client.get(f"/transcript/{sid}/docx")
    assert rv.status_code == 200
    with app.app_context():
        assert AuditLog.query.filter_by(
            event_type="transcript_downloaded", session_id=sid).count() == 1


def test_progress_page_blocks_other_users(enc_client):
    """The progress page may only be viewed by its own user (was an IDOR)."""
    with enc_client.session_transaction() as s:
        s["user_id"] = "user-A"
    assert enc_client.get("/progress/user-B/solo").status_code == 403   # someone else's
    assert enc_client.get("/progress/user-A/solo").status_code == 200   # your own


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
    assert "Clinician summary" in text
    assert "F43.2" in text                       # codes still present
    assert "narrative unavailable" in text.lower()
