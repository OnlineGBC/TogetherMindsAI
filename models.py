import hashlib
import hmac
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy_utils import StringEncryptedType
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


class _GracefulEncryptedType(StringEncryptedType):
    """Field encryption that tolerates legacy plaintext on read.

    When a previously-plaintext column is switched to encrypted, existing rows
    still hold plaintext that a normal EncryptedType can't decrypt (it raises).
    This subclass returns the raw stored value unchanged when decryption fails,
    so reads keep working while the one-time backfill re-encrypts rows in place.
    New writes are always encrypted. Once the backfill has run, every row is
    ciphertext and this fallback is never taken."""

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        try:
            return super().process_result_value(value, dialect)
        except Exception:
            return value   # legacy plaintext not yet re-encrypted by the backfill


def friendly_name_key(name):
    """Deterministic lookup key for an (encrypted) session friendly name.

    friendly_name is stored encrypted and non-deterministic, so it can't be
    queried or uniqueness-checked in the database directly. This HMAC of the
    normalised name is stored beside it: the same name always yields the same
    key (so we can find it and enforce uniqueness) without revealing the name.
    Case-insensitive, matching the historical friendly-name lookup."""
    if not name:
        return None
    norm = name.strip().upper().encode("utf-8")
    secret = (_encryption_key[0] or "").encode("utf-8")
    return hmac.new(secret, norm, hashlib.sha256).hexdigest()


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
    # e.g. "Michael" — often a real first name, so encrypted at rest. null for AI rows.
    display_name = db.Column(_GracefulEncryptedType(db.Text, lambda: _encryption_key[0], FernetEngine), nullable=True)
    text = db.Column(StringEncryptedType(db.Text, lambda: _encryption_key[0], FernetEngine), nullable=False)
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


class Clinician(db.Model):
    """A logged-in therapist account, authenticated via Google or Microsoft OAuth.

    Deliberately stores NO email / PII — only the provider's opaque, stable subject
    id. `id` (our own UUID) is what owns therapist-led sessions (TherapySession.
    therapist_id / created_by). Google and Microsoft logins are separate accounts
    (we don't store email to link them).
    """
    __tablename__ = "clinicians"

    id               = db.Column(db.String(36), primary_key=True)            # our UUID
    provider         = db.Column(db.String(20),  nullable=False)             # "google" | "microsoft"
    provider_subject = db.Column(db.String(255), nullable=False)             # provider's stable user id ("sub")
    # Encrypted at rest. Captured from the OAuth "email" claim, used only to send
    # the clinician their own session-recording links + retention notices (Phase 4
    # Step 3). Nullable: pre-existing accounts have none until they next log in.
    email            = db.Column(StringEncryptedType(db.Text, lambda: _encryption_key[0], FernetEngine), nullable=True)
    created_at       = db.Column(db.DateTime, nullable=False)
    last_login_at    = db.Column(db.DateTime, nullable=True)
    # What kind of practitioner this is: psychotherapist | hypnotherapist | caregiver.
    # Decides which features exist for them at all, before plan is even considered
    # (see roles.py). NULL means "not chosen yet" — such an account behaves as a
    # psychotherapist, which is how the app worked before roles existed, so a missing
    # role can never quietly take something away. Only an admin may change it.
    role             = db.Column(db.String(32), nullable=True)
    # Subscription billing (Phase 4 Step 4 — Stripe). plan is the entitlement tier:
    # "free" | "pro" ($10, AI analysis) | "premium" ($25, + recording). A NULL/absent
    # plan is treated as "free". subscription_status mirrors Stripe (active,
    # trialing, past_due, canceled, …); only active/trialing grant the paid tier.
    stripe_customer_id  = db.Column(db.String(64), nullable=True)
    plan                = db.Column(db.String(16), nullable=True)
    subscription_status = db.Column(db.String(24), nullable=True)
    current_period_end  = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("provider", "provider_subject", name="uq_clinician_provider_subject"),
    )

    def __repr__(self):
        return f"<Clinician {self.id} provider={self.provider}>"


class ClientAccount(db.Model):
    """An optional logged-in client account, authenticated via Google or Microsoft.

    Lets a client find the therapist-led sessions they took part in across devices,
    instead of relying on a browser-cookie UUID. Like Clinician, it deliberately
    stores NO email / PII — only the provider's opaque, stable subject id. It is a
    SEPARATE account type from Clinician (a client login can never become a
    clinician), and login is always optional — anonymous join still works.
    """
    __tablename__ = "client_accounts"

    id               = db.Column(db.String(36), primary_key=True)            # our UUID (used as the client's user_id)
    provider         = db.Column(db.String(20),  nullable=False)             # "google" | "microsoft"
    provider_subject = db.Column(db.String(255), nullable=False)             # provider's stable user id ("sub")
    created_at       = db.Column(db.DateTime, nullable=False)
    last_login_at    = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("provider", "provider_subject", name="uq_client_provider_subject"),
    )

    def __repr__(self):
        return f"<ClientAccount {self.id} provider={self.provider}>"


class TherapySession(db.Model):
    __tablename__ = "therapy_sessions"

    id = db.Column(db.String(36), primary_key=True)         # randomized private key session ID (same format for all modes)
    mode = db.Column(db.String(20), nullable=False)          # "solo", "couple", or "group"
    created_by = db.Column(db.String(36), nullable=False)    # user_id of creator
    created_at = db.Column(db.DateTime, nullable=False)
    retention_expires_at = db.Column(db.DateTime, nullable=True)  # auto-purge after 30 days
    # When set, the session is therapist-led: this user_id is the licensed
    # professional leading it. The AI then acts as a private co-pilot (suggestion
    # cards to the therapist only) instead of replying to clients. Null = the
    # original AI-led consumer flow, unchanged.
    therapist_id = db.Column(db.String(36), nullable=True)
    # Shared, therapist-set friendly name for the session. Unique across sessions
    # so a participant can rejoin by it (or by the Session ID). Persisted so it
    # survives restarts.
    # Encrypted at rest (may embed a client's real name, e.g. "Smith weekly").
    # Because ciphertext is non-deterministic it can't be queried or made unique
    # directly — friendly_name_key (below) carries the deterministic lookup +
    # uniqueness instead.
    friendly_name = db.Column(_GracefulEncryptedType(db.Text, lambda: _encryption_key[0], FernetEngine), nullable=True)
    # HMAC of the normalised friendly name — deterministic, so it is queryable
    # and unique. See models.friendly_name_key().
    friendly_name_key = db.Column(db.String(64), nullable=True, unique=True, index=True)
    # Opaque token for tokenized transcript download links in the end-session email,
    # so the session id never appears in the URL (like SessionRecording.download_token).
    download_token = db.Column(db.String(64), nullable=True, index=True)
    # Heartbeat presence: the therapist's page POSTs /heartbeat every ~15s, updating
    # this. A client is admitted from the waiting room only if the therapist was seen
    # recently. DB-backed so presence survives restarts and doesn't flap with sockets.
    therapist_last_seen = db.Column(db.DateTime, nullable=True)


class SessionParticipant(db.Model):
    """Records that a user_id took part in a session, written at join time.

    This is what lets a signed-in client's session appear in "my sessions" even
    if they never sent a message (silent attendee). One row per (session, user);
    joins are idempotent. Stores no content — only the participation link.
    """
    __tablename__ = "session_participants"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.String(36), index=True, nullable=False)
    user_id    = db.Column(db.String(36), index=True, nullable=False)
    joined_at  = db.Column(db.DateTime, nullable=False)
    # The participant's self-chosen display name, persisted so it is restored when
    # the same user (same browser id, or signed-in account) leaves and rejoins.
    # Often a real first name → encrypted at rest.
    display_name = db.Column(_GracefulEncryptedType(db.Text, lambda: _encryption_key[0], FernetEngine), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("session_id", "user_id", name="uq_session_participant"),
    )

    def __repr__(self):
        return f"<SessionParticipant session={self.session_id} user={self.user_id}>"


class SessionStateCert(db.Model):
    """The clinician's per-session attestation about a client's U.S. state.

    Telehealth licensure follows the client's PHYSICAL location at session time,
    so before a client is admitted the clinician certifies (once per state) that
    they are authorised to provide services to a client located there — via a
    state licence or an applicable interstate compact. One row per (session,
    state): the first client from a state triggers it; the rest are covered by
    the same row.

    decision is 'certified' (admit clients from that state this session) or
    'declined' (turn them away). This is the audit-grade licensure record; the
    same event is also written to the tamper-evident audit log.
    """
    __tablename__ = "session_state_certs"

    id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id   = db.Column(db.String(36), index=True, nullable=False)
    # Location code: a U.S. state (e.g. "NJ") or a country (prefixed, "C:FR").
    state        = db.Column(db.String(8), nullable=False)
    therapist_id = db.Column(db.String(36), nullable=False)
    decision     = db.Column(db.String(10), nullable=False)     # certified | declined
    attested_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("session_id", "state", name="uq_session_state_cert"),
    )

    def __repr__(self):
        return f"<SessionStateCert session={self.session_id} state={self.state} {self.decision}>"


class NotificationLog(db.Model):
    """Durable ledger of one-shot/annual notifications already sent.

    A unique (key, year) row is the claim token: whichever process inserts it
    first owns the send, so a notification goes out exactly once per year even
    across multiple instances and restarts (e.g. the annual ICD-refresh email,
    fired by either the March-1 cron or the startup catch-up).
    """
    __tablename__ = "notification_log"

    id      = db.Column(db.Integer, primary_key=True, autoincrement=True)
    key     = db.Column(db.String(64), nullable=False)
    year    = db.Column(db.Integer, nullable=False)
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("key", "year", name="uq_notification_key_year"),
    )

    def __repr__(self):
        return f"<NotificationLog {self.key} {self.year}>"


class CopilotCard(db.Model):
    """A persisted therapist co-pilot card (suggestion, risk, or reference).

    Cards used to be ephemeral — emitted over SocketIO with only a count logged —
    so anything beyond the console's in-memory window was lost on scroll, reload,
    or server restart. Storing them lets the therapist console replay the full
    history on (re)connect.

    `text` and `payload` are encrypted because a card can quote client content.
    `payload` is the full emitted card dict as JSON, so the console re-renders
    exactly what was shown (code links, source, priority included).

    Lifecycle: purged with the parent session (retention), and erased in the
    GDPR delete-user flow for any session the deleted user took part in (a card
    may quote that person's now-erased words).
    """
    __tablename__ = "copilot_cards"

    id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id      = db.Column(db.String(36), index=True, nullable=False)
    card_type       = db.Column(db.String(20), nullable=False)   # question|technique|observation|risk|reference
    text            = db.Column(StringEncryptedType(db.Text, lambda: _encryption_key[0], FernetEngine), nullable=False)
    payload         = db.Column(StringEncryptedType(db.Text, lambda: _encryption_key[0], FernetEngine), nullable=False)
    confidence      = db.Column(db.Float, nullable=True)
    trigger_user_id = db.Column(db.String(36), nullable=True)    # who spoke the turn that produced the card
    created_at      = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    def __repr__(self):
        return f"<CopilotCard id={self.id} session={self.session_id} type={self.card_type}>"


class SessionSummary(db.Model):
    """Cached therapist-only session summary (clinical recap + grounded ICD codes
    + client-facing draft).

    Generating it is a multi-second LLM call, so it is cached per session and
    reused while the conversation is unchanged — keyed on the message count it
    covers, so the next new message triggers a fresh generation (never stale).
    The payload (the full summary dict as JSON) is encrypted because it quotes
    client content; it is purged with the session and erased in the GDPR
    delete-user flow.
    """
    __tablename__ = "session_summaries"

    session_id    = db.Column(db.String(36), primary_key=True)
    payload       = db.Column(StringEncryptedType(db.Text, lambda: _encryption_key[0], FernetEngine), nullable=False)
    message_count = db.Column(db.Integer, nullable=False)   # cache key: messages the summary covers
    generated_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    def __repr__(self):
        return f"<SessionSummary session={self.session_id} msgs={self.message_count}>"


class SessionHidden(db.Model):
    """Marks that a participant hid a (therapist-led) session from THEIR OWN view.

    For clinical records, the clinician must retain the session (medical-record
    retention law), so a participant's "delete my data" cannot erase it. Instead
    we record that this user hid it: the session no longer appears in, or is
    retrievable by, that user — while the clinician's copy is untouched. Stores no
    content (just the link), and is cleared when the session is finally purged.
    """
    __tablename__ = "session_hidden"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.String(36), index=True, nullable=False)
    user_id    = db.Column(db.String(36), index=True, nullable=False)
    hidden_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("session_id", "user_id", name="uq_session_hidden"),
    )

    def __repr__(self):
        return f"<SessionHidden session={self.session_id} user={self.user_id}>"


class SessionRecording(db.Model):
    """Metadata for a recorded session (Phase 4 — optional paid A/V recording).

    The video file itself lives in the private recordings GCS bucket; this row
    tracks the recording's lifecycle (status, the LiveKit egress job id, the
    object path, who started it, and when). No PHI content is stored here.
    """
    __tablename__ = "session_recordings"

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id  = db.Column(db.String(36), index=True, nullable=False)
    egress_id   = db.Column(db.String(64), nullable=True)     # LiveKit egress job id
    gcs_object  = db.Column(db.String(512), nullable=True)    # path within the recordings bucket
    status      = db.Column(db.String(20), nullable=False)    # active | stopped | failed | deleted
    started_by  = db.Column(db.String(36), nullable=True)     # clinician user_id
    started_at  = db.Column(db.DateTime, nullable=False)
    stopped_at  = db.Column(db.DateTime, nullable=True)
    retention_expires_at = db.Column(db.DateTime, nullable=True)  # 30-day delete (Phase 4 Step 3)
    # Two warnings, each sent at most once: an early "about a week left" notice and
    # a final notice. A recording is not deleted until the final notice has gone out
    # (see _recording_retention_sweep), so neither may be reused for the other.
    early_reminder_sent_at = db.Column(db.DateTime, nullable=True)  # 7-days-before email
    reminder_sent_at     = db.Column(db.DateTime, nullable=True)  # final (48h) email
    # Opaque, unguessable download token. The download URL is keyed on this, NOT on
    # the session id, so the session id never appears in a URL / email / browser
    # history / referrer. Still therapist-gated on top of the token.
    download_token       = db.Column(db.String(64), nullable=True, unique=True, index=True)

    def __repr__(self):
        return f"<SessionRecording id={self.id} session={self.session_id} status={self.status}>"


class HoursGrant(db.Model):
    """A block of recording time given to a caregiver account.

    Two kinds, with different lifetimes:
      * "monthly" — the 40 hours included in the plan. Resets each month and does
        NOT carry over.
      * "topup"   — a $9.99 purchase of 40 more hours. Carries over to the end of
        the FOLLOWING month, then expires.

    A ledger rather than a running total on the account, because hours arrive with
    different expiry dates. Consumption records against the individual grant it
    came out of, so a carried-over block cannot be silently counted twice.

    Minutes, not hours: recordings are not whole hours, and integers avoid the
    rounding drift that would come from storing fractions.
    """
    __tablename__ = "hours_grants"

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    clinician_id  = db.Column(db.String(36), index=True, nullable=False)
    kind          = db.Column(db.String(10), nullable=False)      # monthly | topup
    minutes       = db.Column(db.Integer, nullable=False)         # granted
    used_minutes  = db.Column(db.Integer, nullable=False, default=0)
    granted_at    = db.Column(db.DateTime, nullable=False)
    expires_at    = db.Column(db.DateTime, nullable=False)
    # Stripe payment for a top-up, so a purchase cannot be credited twice if the
    # webhook is delivered more than once.
    stripe_ref    = db.Column(db.String(64), unique=True, nullable=True)

    def __repr__(self):
        return (f"<HoursGrant {self.id} {self.kind} "
                f"{self.used_minutes}/{self.minutes}m>")


class RecordAuthorisation(db.Model):
    """A caregiver's confirmation that they may record the person in a session.

    The person being recorded — a baby, a patient — often cannot consent for
    themselves, so the caregiver attests that they hold the authority instead.
    Stored rather than kept in the browser session because it is a legal
    attestation: who confirmed what, and when, has to survive.

    One row per session; recording is refused until it exists.
    """
    __tablename__ = "record_authorisations"

    id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id   = db.Column(db.String(36), index=True, nullable=False)
    clinician_id = db.Column(db.String(36), nullable=False)
    confirmed_at = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("session_id", name="uq_record_auth_session"),
    )

    def __repr__(self):
        return f"<RecordAuthorisation session={self.session_id}>"


class CompAccess(db.Model):
    """An email granted full (premium) access without paying.

    Keyed on email_hash, NOT on the email itself: Clinician.email is Fernet-
    encrypted, which produces different ciphertext every time, so an equality
    query on the plaintext can never match. email_hash is a deterministic HMAC of
    the lowercased address (see admin_access.email_hash), which both makes the
    lookup possible and lets an address be comped BEFORE that person has ever
    signed up — it takes effect the moment they log in.

    Revoking sets revoked_at rather than deleting the row, so the grant history
    survives for audit.
    """
    __tablename__ = "comp_access"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email_hash = db.Column(db.String(64), unique=True, index=True, nullable=False)
    # Shown back in the admin list so a grant is recognisable. Encrypted at rest
    # like every other stored address.
    email      = db.Column(StringEncryptedType(db.Text, lambda: _encryption_key[0], FernetEngine), nullable=True)
    note       = db.Column(db.String(200), nullable=True)
    added_by   = db.Column(db.String(255), nullable=True)      # admin email
    created_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)         # NULL = active

    def __repr__(self):
        state = "revoked" if self.revoked_at else "active"
        return f"<CompAccess id={self.id} {state}>"


class AdminAuthCode(db.Model):
    """A one-time code sent to an admin by email or SMS for the 2-of-3 challenge.

    The code is stored hashed, never in the clear. Rows are single-use, expire,
    and carry an attempt counter so a code cannot be brute-forced.
    """
    __tablename__ = "admin_auth_codes"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_hash = db.Column(db.String(64), index=True, nullable=False)  # HMAC of admin email
    channel    = db.Column(db.String(10), nullable=False)              # email | sms
    code_hash  = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at    = db.Column(db.DateTime, nullable=True)
    attempts   = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f"<AdminAuthCode id={self.id} channel={self.channel}>"
