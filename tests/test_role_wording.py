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
# The disclaimer bar, which every visitor sees
# ---------------------------------------------------------------------------

def test_the_bar_matches_the_signed_in_practitioner(client):
    with app.app_context():
        _clinician(role=roles.HYPNOTHERAPIST)
    _login(client)
    body = client.get("/billing").get_data(as_text=True)
    assert "supporting your practitioner" in body
    assert "supporting your clinician" not in body


def test_a_caregiver_is_not_told_about_clinical_care(client):
    with app.app_context():
        _clinician(role=roles.CAREGIVER)
    _login(client)
    body = client.get("/billing").get_data(as_text=True)
    assert "helps you watch and record" in body
    assert "your clinician" not in body


def test_crisis_numbers_show_for_every_role(client):
    """These sit outside the wording switch on purpose. A wording change must
    never be able to remove them."""
    for role in roles.ROLES:
        with app.app_context():
            db.session.query(Clinician).delete()
            db.session.commit()
            _clinician(role=role)
        _login(client)
        body = client.get("/billing").get_data(as_text=True)
        assert "988" in body, role
        assert "741741" in body, role
        assert "findahelpline.com" in body, role


def test_signed_out_visitors_still_get_the_clinical_wording(client):
    """No account, so nothing to key on — keep the wording the app had before."""
    body = client.get("/welcome").get_data(as_text=True)
    assert "supporting your clinician" in body


def test_a_client_sees_the_wording_of_the_practitioner_running_the_session(client):
    """The client's own account type is irrelevant — what matters is who they are
    actually seeing."""
    with app.app_context():
        _clinician("coach", role=roles.HYPNOTHERAPIST)
        now = datetime.now(timezone.utc)
        db.session.add(TherapySession(
            id="s1", mode="solo", created_by="coach", created_at=now,
            retention_expires_at=now + timedelta(days=30), therapist_id="coach"))
        db.session.commit()
    with client.session_transaction() as s:
        s["user_id"] = "client-1"             # a client, not the practitioner
    body = client.get("/session/s1/consent").get_data(as_text=True)
    assert "supporting your practitioner" in body
    assert "supporting your clinician" not in body


# ---------------------------------------------------------------------------
# The legal pages
# ---------------------------------------------------------------------------

def _flat(html: str) -> str:
    """Collapse whitespace, so a phrase split across source lines still matches."""
    import re
    return re.sub(r"\s+", " ", html)


def test_privacy_explains_that_hipaa_is_clinical_only(client):
    """These pages are read signed out, so they stay static — a reader with no
    account has no role. The split is explained instead of switched."""
    body = _flat(client.get("/privacy").get_data(as_text=True))
    assert "Accounts that are not clinical" in body
    assert "technical protections are identical for every account" in body
    assert "not acting as a HIPAA business associate" in body


def test_terms_scope_the_clinical_claims(client):
    body = _flat(client.get("/tos").get_data(as_text=True))
    assert "Not every account is clinical" in body
    assert "creates no clinical record" in body


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
