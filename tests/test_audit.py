"""
tests/test_audit.py
-------------------
Tests for the three-tier tamper-evident audit logging system.

Tier 1: log_event inserts an append-only DB row
Tier 2: SHA-256 hash chain is valid; verify_audit_chain() detects modification/deletion
Tier 3: structured JSON emitted via logger → external sink (GCP Cloud Logging)
"""

import os
import sys
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-audit")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

from cryptography.fernet import Fernet
TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from TogetherMindsAI import app
from models import db, AuditLog, init_encryption
from audit import log_event, verify_audit_chain

init_encryption(TEST_KEY)


# ---------------------------------------------------------------------------
# Fixture — isolated in-memory DB per test
# ---------------------------------------------------------------------------

@pytest.fixture
def audit_ctx():
    """Yield an active app context backed by a fresh in-memory SQLite DB."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db._app_engines[app] = {None: test_engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


# ---------------------------------------------------------------------------
# Tier 1 — append-only DB row
# ---------------------------------------------------------------------------

class TestTier1AppendOnly:

    def test_log_event_creates_row(self, audit_ctx):
        log_event("test_event", session_id="abc123", user_id="user1", detail="value")
        assert AuditLog.query.count() == 1
        row = AuditLog.query.first()
        assert row.event_type == "test_event"
        assert row.session_id == "abc123"
        assert row.user_id == "user1"

    def test_multiple_events_all_stored(self, audit_ctx):
        log_event("session_created", session_id="s1", mode="solo")
        log_event("message_sent", session_id="s1", message_length=42)
        log_event("session_deleted_user", session_id="s1", trigger="user")
        assert AuditLog.query.count() == 3

    def test_details_stored_as_json(self, audit_ctx):
        log_event("message_sent", session_id="s1", message_length=99, mode="solo")
        row = AuditLog.query.first()
        details = json.loads(row.details)
        assert details["message_length"] == 99
        assert details["mode"] == "solo"

    def test_no_message_content_in_details(self, audit_ctx):
        log_event("message_sent", session_id="s1", message_length=99)
        row = AuditLog.query.first()
        raw = row.details or ""
        assert "text" not in raw
        assert "content" not in raw
        assert "message" not in raw.lower() or "message_length" in raw

    def test_event_without_session_or_user(self, audit_ctx):
        log_event("system_startup")
        row = AuditLog.query.first()
        assert row.session_id is None
        assert row.user_id is None


# ---------------------------------------------------------------------------
# Tier 2 — SHA-256 hash chain
# ---------------------------------------------------------------------------

class TestTier2HashChain:

    def test_genesis_row_uses_zero_prev_hash(self, audit_ctx):
        log_event("first_event")
        row = AuditLog.query.first()
        assert row.prev_hash == "0" * 64

    def test_second_row_prev_hash_matches_first_row_hash(self, audit_ctx):
        log_event("event_one")
        log_event("event_two")
        rows = AuditLog.query.order_by(AuditLog.id.asc()).all()
        assert rows[1].prev_hash == rows[0].row_hash

    def test_chain_links_all_rows_in_sequence(self, audit_ctx):
        for i in range(5):
            log_event("event", seq=i)
        rows = AuditLog.query.order_by(AuditLog.id.asc()).all()
        for i in range(1, len(rows)):
            assert rows[i].prev_hash == rows[i - 1].row_hash

    def test_verify_chain_passes_on_clean_log(self, audit_ctx):
        log_event("session_created", session_id="s1", mode="solo")
        log_event("message_sent", session_id="s1", message_length=50)
        log_event("session_deleted_user", session_id="s1", trigger="user")
        valid, result = verify_audit_chain()
        assert valid is True
        assert result == 3

    def test_verify_chain_empty_table_returns_true(self, audit_ctx):
        valid, result = verify_audit_chain()
        assert valid is True
        assert result == 0

    def test_verify_chain_detects_row_hash_tampering(self, audit_ctx):
        log_event("event_a")
        log_event("event_b")
        row = AuditLog.query.order_by(AuditLog.id.asc()).first()
        row.row_hash = "tampered" + "0" * 56  # corrupt the first row's hash
        db.session.commit()
        valid, broken_id = verify_audit_chain()
        assert valid is False
        assert broken_id == row.id

    def test_verify_chain_detects_prev_hash_tampering(self, audit_ctx):
        log_event("event_x")
        log_event("event_y")
        second = AuditLog.query.order_by(AuditLog.id.desc()).first()
        second.prev_hash = "0" * 64  # break chain: pretend no predecessor
        db.session.commit()
        valid, broken_id = verify_audit_chain()
        assert valid is False
        assert broken_id == second.id

    def test_verify_chain_detects_field_edit(self, audit_ctx):
        log_event("message_sent", session_id="s1", message_length=10)
        row = AuditLog.query.first()
        row.details = '{"message_length": 9999}'  # edit without updating hash
        db.session.commit()
        valid, broken_id = verify_audit_chain()
        assert valid is False
        assert broken_id == row.id


# ---------------------------------------------------------------------------
# Tier 3 — external sink (structured JSON via logger)
# ---------------------------------------------------------------------------

class TestTier3ExternalSink:

    def test_log_event_emits_structured_json(self, audit_ctx):
        with patch("audit.logger") as mock_logger:
            log_event("crisis_detected", session_id="s1")
        mock_logger.info.assert_called_once()
        logged = json.loads(mock_logger.info.call_args[0][0])
        assert logged["audit"] is True
        assert logged["event"] == "crisis_detected"
        assert logged["session_id"] == "s1"
        assert "timestamp" in logged
        assert "row_hash" in logged

    def test_structured_log_contains_no_pii(self, audit_ctx):
        with patch("audit.logger") as mock_logger:
            log_event("message_sent", session_id="s1", message_length=80)
        logged = json.loads(mock_logger.info.call_args[0][0])
        dumped = json.dumps(logged)
        assert "message_length" in dumped
        # No raw text, no names, no identifiable content beyond session/user IDs
        assert "text" not in dumped
        assert "content" not in dumped

    def test_structured_log_emitted_even_after_db_insert(self, audit_ctx):
        """logger.info is always called regardless of DB state."""
        with patch("audit.logger") as mock_logger:
            log_event("session_created", session_id="s2", mode="couple")
        assert mock_logger.info.call_count == 1
