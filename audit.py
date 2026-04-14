"""
audit.py
--------
Three-tier tamper-evident audit logging (HIPAA § 164.312(b)).

Tier 1 — Append-only DB table  (AuditLog model; no UPDATE/DELETE routes exist)
Tier 2 — SHA-256 hash chain    (each row hashes all its fields + previous row's hash)
Tier 3 — External sink          (structured JSON via logger → stdout → GCP Cloud Logging)

Retention: 6 years per HIPAA requirement.
Message content is NEVER stored — metadata only (lengths, modes, trigger types).
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Sentinel prev_hash for the first (genesis) row — no predecessor
_GENESIS_HASH = "0" * 64


def log_event(event_type: str, session_id: str = None, user_id: str = None, **details) -> None:
    """Insert a tamper-evident audit row and emit a structured log entry.

    Must be called with an active Flask app context.  All HTTP route handlers
    and the scheduler purge job satisfy this requirement.

    Args:
        event_type: Short identifier, e.g. 'session_created', 'crisis_detected'.
        session_id: Therapy session ID (anonymised 6-char code, not a UUID).
        user_id:    Internal user UUID.  Never exposed to other participants.
        **details:  Arbitrary metadata — no message content, no PII allowed.
    """
    from models import db, AuditLog

    now = datetime.now(timezone.utc)
    details_json = json.dumps(details, sort_keys=True) if details else "{}"
    timestamp_iso = now.isoformat()

    # Tier 2 — retrieve the previous row's hash to extend the chain
    last = AuditLog.query.order_by(AuditLog.id.desc()).first()
    prev_hash = last.row_hash if last else _GENESIS_HASH

    # SHA-256 over every field in insertion order — detects any post-insert edit
    payload = (
        f"{event_type}|{session_id or ''}|{user_id or ''}|"
        f"{details_json}|{timestamp_iso}|{prev_hash}"
    )
    row_hash = hashlib.sha256(payload.encode()).hexdigest()

    # Tier 1 — append to DB
    row = AuditLog(
        event_type=event_type,
        session_id=session_id,
        user_id=user_id,
        details=details_json,
        prev_hash=prev_hash,
        row_hash=row_hash,
        timestamp=now,
        timestamp_str=timestamp_iso,   # exact string used in hash — survives DB round-trip
    )
    db.session.add(row)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("audit log DB insert failed (%s); emitting to external sink only.", exc)

    # Tier 3 — structured JSON to stdout; Cloud Run forwards to GCP Cloud Logging
    _emit_external(event_type, session_id, user_id, details, timestamp_iso, row_hash)


def _emit_external(event_type, session_id, user_id, details, timestamp_iso, row_hash):
    """Emit a structured JSON line to the log sink (stdout on Cloud Run)."""
    logger.info(json.dumps({
        "audit": True,
        "event": event_type,
        "session_id": session_id,
        "user_id": user_id,
        "details": details,
        "timestamp": timestamp_iso,
        "row_hash": row_hash,
    }))


def verify_audit_chain():
    """Walk every audit row in insertion order and verify the hash chain.

    Recomputes the SHA-256 hash for each row and checks that each row's
    prev_hash matches the preceding row's row_hash.  Any modification or
    deletion of a row will produce a mismatch.

    Returns:
        (True,  n_rows)     — chain is intact
        (False, broken_id)  — row with this id is the first with a broken hash
    """
    from models import AuditLog

    rows = AuditLog.query.order_by(AuditLog.id.asc()).all()
    prev_hash = _GENESIS_HASH

    for row in rows:
        # Check chain linkage
        if row.prev_hash != prev_hash:
            return False, row.id

        # Recompute expected hash using the stored ISO string (survives DB round-trip)
        payload = (
            f"{row.event_type}|{row.session_id or ''}|{row.user_id or ''}|"
            f"{row.details or '{}'}|{row.timestamp_str}|{row.prev_hash}"
        )
        expected = hashlib.sha256(payload.encode()).hexdigest()
        if row.row_hash != expected:
            return False, row.id

        prev_hash = row.row_hash

    return True, len(rows)
