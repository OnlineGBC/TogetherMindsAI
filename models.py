from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy_utils import EncryptedType
from sqlalchemy_utils.types.encrypted.encrypted_type import FernetEngine

db = SQLAlchemy()

# Populated by init_encryption() called from TogetherMindsAI.py after config is loaded.
# Using a mutable container so the reference inside EncryptedType columns stays live.
_encryption_key: list = [""]


def init_encryption(key: str) -> None:
    """Store the field encryption key so EncryptedType columns can use it.

    Must be called once at app startup before any DB read/write.
    WARNING: never rotate this key without first re-encrypting all existing
    ChatMessage rows — otherwise stored messages become permanently unreadable.
    """
    _encryption_key[0] = key


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(36), primary_key=True)
    passphrase_hash = db.Column(db.String(256), nullable=True)   # deprecated, kept for migration
    therapy_mode = db.Column(db.String(20), nullable=False)
    public_key = db.Column(db.Text, nullable=True)               # base64-encoded SPKI DER
    challenge = db.Column(db.String(64), nullable=True)          # current auth nonce
    challenge_expires_at = db.Column(db.Float, nullable=True)    # Unix timestamp

    def __repr__(self):
        return f"<User {self.id} mode={self.therapy_mode}>"


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.String(36), index=True, nullable=False)
    user_id = db.Column(db.String(36), nullable=False)
    display_name = db.Column(db.String(60), nullable=True)   # e.g. "Michael"; null for AI and legacy rows
    text = db.Column(EncryptedType(db.Text, lambda: _encryption_key[0], FernetEngine), nullable=False)
    timestamp = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    def __repr__(self):
        return f"<ChatMessage id={self.id} session={self.session_id} user={self.user_id}>"

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "text": self.text,
            "timestamp": self.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        }


class Exercise(db.Model):
    __tablename__ = "exercises"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.String(36), index=True, nullable=False)
    type = db.Column(db.String(50), nullable=False)
    mode = db.Column(db.String(20), nullable=True)               # "solo", "couple", "group"
    timestamp = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    def __repr__(self):
        return f"<Exercise id={self.id} user={self.user_id} type={self.type}>"


class AuditLog(db.Model):
    """Tamper-evident, append-only security audit log (HIPAA § 164.312(b)).

    Rows are never updated or deleted by application code.
    A SHA-256 hash chain links every row to its predecessor so any
    modification or deletion is detectable by verify_audit_chain().
    Retention: 6 years (purged by the scheduler, not on user request).
    """
    __tablename__ = "audit_logs"

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_type    = db.Column(db.String(64),  nullable=False, index=True)
    session_id    = db.Column(db.String(36),  nullable=True,  index=True)
    user_id       = db.Column(db.String(36),  nullable=True)
    details       = db.Column(db.Text,        nullable=True)          # JSON metadata — no message content
    prev_hash     = db.Column(db.String(64),  nullable=False)         # hash of preceding row
    row_hash      = db.Column(db.String(64),  nullable=False, unique=True)
    timestamp     = db.Column(db.DateTime,    nullable=False)
    timestamp_str = db.Column(db.String(40),  nullable=False)         # exact ISO string used in hash

    def __repr__(self):
        return f"<AuditLog id={self.id} event={self.event_type} ts={self.timestamp}>"


class RateLimitEntry(db.Model):
    __tablename__ = "rate_limit_entries"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.String(36), index=True, nullable=False)
    timestamp = db.Column(db.Float, nullable=False)   # Unix epoch float


class TherapySession(db.Model):
    __tablename__ = "therapy_sessions"

    id = db.Column(db.String(36), primary_key=True)         # 6-char mixed-case alphanumeric session ID (same format for all modes)
    mode = db.Column(db.String(20), nullable=False)          # "solo", "couple", or "group"
    created_by = db.Column(db.String(36), nullable=False)    # user_id of creator
    created_at = db.Column(db.DateTime, nullable=False)
    retention_expires_at = db.Column(db.DateTime, nullable=True)  # auto-purge after 30 days
