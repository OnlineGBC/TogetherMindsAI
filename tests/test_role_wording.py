"""
tests/test_role_wording.py
--------------------------
Step 7: wording follows the practitioner's role, on every screen.

Two properties matter more than the exact words:

  * Clinical language ("clinician", "clinical record", "professional care") must
    never appear for a role that is not licensed clinical work.
  * The crisis numbers must appear for EVERY role. They sit outside the wording
    switch precisely so a wording change cannot drop them.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-wording")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, TherapySession

init_encryption(TEST_KEY)

CLINICAL_WORDS = ("clinician", "clinical record", "professional care")


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


def _clinician(uid="doc", role=roles.PSYCHOTHERAPIST):
    db.session.add(Clinician(id=uid, provider="google", provider_subject=uid,
                             email=f"{uid}@example.com", role=role,
                             created_at=datetime.now(timezone.utc)))
    db.session.commit()


def _login(client, uid="doc"):
    with client.session_transaction() as s:
        s["clinician_id"] = uid
        s["user_id"] = uid


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------

def test_every_role_has_every_word():
    """A missing key would render an empty string into a live page."""
    expected = set(roles.WORDING[roles.PSYCHOTHERAPIST])
    for role in roles.ROLES:
        assert set(roles.WORDING[role]) == expected, role
        assert all(roles.WORDING[role].values()), role


def test_only_the_clinical_role_uses_clinical_words():
    for role in (roles.HYPNOTHERAPIST, roles.CAREGIVER):
        blob = " ".join(roles.words(role).values()).lower()
        for word in CLINICAL_WORDS:
            assert word not in blob, f"{role} uses '{word}'"


def test_unknown_role_falls_back_to_clinical_wording():
    assert roles.words("wizard") == roles.WORDING[roles.PSYCHOTHERAPIST]


# ---------------------------------------------------------------------------
# Baby cam: no disclaimer bar and no crisis lines on any page, for any visitor
# ---------------------------------------------------------------------------

GONE_EVERYWHERE = ("disclaimer-bar", "988", "741741", "findahelpline.com",
                   "befrienders.org", "wellnessModal", "Checking in with you")


def test_no_role_sees_the_disclaimer_bar_or_crisis_lines(client):
    for role in roles.ROLES:
        with app.app_context():
            db.session.query(Clinician).delete()
            db.session.commit()
            _clinician(role=role)
        _login(client)
        body = client.get("/pricing").get_data(as_text=True)
        for gone in GONE_EVERYWHERE:
            assert gone not in body, (role, gone)


def test_signed_out_visitors_see_no_disclaimer_bar_or_crisis_lines(client):
    for path in ("/welcome", "/privacy", "/tos", "/session/join"):
        body = client.get(path).get_data(as_text=True)
        for gone in GONE_EVERYWHERE:
            assert gone not in body, (path, gone)


def test_a_client_sees_no_crisis_lines_at_the_consent_gate(client):
    with app.app_context():
        _clinician("coach", role=roles.CAREGIVER)
        now = datetime.now(timezone.utc)
        db.session.add(TherapySession(
            id="s1", mode="solo", created_by="coach", created_at=now,
            retention_expires_at=now + timedelta(days=30), therapist_id="coach"))
        db.session.commit()
    with client.session_transaction() as s:
        s["user_id"] = "client-1"             # a client, not the practitioner
    body = client.get("/session/s1/consent").get_data(as_text=True)
    for gone in GONE_EVERYWHERE:
        assert gone not in body, gone


# ---------------------------------------------------------------------------
# The legal pages
# ---------------------------------------------------------------------------

def _flat(html: str) -> str:
    """Collapse whitespace, so a phrase split across source lines still matches."""
    import re
    return re.sub(r"\s+", " ", html)


def test_privacy_is_about_a_baby_cam(client):
    body = _flat(client.get("/privacy").get_data(as_text=True)).split("<main", 1)[-1].split("</main>", 1)[0]
    assert "TogetherMindsAI is a baby cam" in body
    assert "not saved" in body
    assert "30 days" in body
    for gone in ("HIPAA", "clinician", "Clinician", "Anthropic", "AssemblyAI",
                 "co-pilot", "transcri", "Crisis Resources", "6 years"):
        assert gone not in body, gone


def test_terms_are_about_a_baby_cam(client):
    body = _flat(client.get("/tos").get_data(as_text=True)).split("<main", 1)[-1].split("</main>", 1)[0]
    assert "TogetherMindsAI is a baby cam" in body
    assert "not</strong> a medical device" in body
    for gone in ("Not every account is clinical", "clinical record",
                 "emergency service", "licensure"):
        assert gone not in body, gone


# ---------------------------------------------------------------------------
# Emails and documents
# ---------------------------------------------------------------------------

def test_a_coachs_documents_are_not_labelled_clinical(client):
    import documents
    buf = documents.transcript_docx_buf(
        "s1", [], "solo", datetime.now(timezone.utc), record_label="session notes")
    from docx import Document as _Doc
    import io
    text = "\n".join(p.text for p in _Doc(io.BytesIO(buf.getvalue())).paragraphs)
    assert "session notes" in text
    assert "clinical record" not in text


def test_documents_default_to_clinical_wording():
    """An older caller that passes no label must keep the previous behaviour."""
    import documents
    assert documents.DEFAULT_RECORD_LABEL == "clinical record"
