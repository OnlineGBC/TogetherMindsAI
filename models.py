from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


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
    text = db.Column(db.Text, nullable=False)
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
            "text": self.text,
            "timestamp": self.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        }


class Exercise(db.Model):
    __tablename__ = "exercises"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.String(36), index=True, nullable=False)
    type = db.Column(db.String(50), nullable=False)
    mode = db.Column(db.String(20), nullable=True)               # "solo", "couple", "group"
    prompt = db.Column(db.Text, nullable=False)
    response = db.Column(db.Text, nullable=False)
    timestamp = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    def __repr__(self):
        return f"<Exercise id={self.id} user={self.user_id} type={self.type}>"


class RateLimitEntry(db.Model):
    __tablename__ = "rate_limit_entries"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.String(36), index=True, nullable=False)
    timestamp = db.Column(db.Float, nullable=False)   # Unix epoch float


class TherapySession(db.Model):
    __tablename__ = "therapy_sessions"

    id = db.Column(db.String(36), primary_key=True)         # session_id (UUID or 4-digit code)
    mode = db.Column(db.String(20), nullable=False)          # "couple" or "group"
    created_by = db.Column(db.String(36), nullable=False)    # user_id of creator
    created_at = db.Column(db.DateTime, nullable=False)
