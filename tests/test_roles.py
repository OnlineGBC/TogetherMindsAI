"""
tests/test_roles.py
-------------------
Step 1 of the roles work: the role table and the account field.

Nothing is gated by role yet — that is Step 2. What these tests pin down is the
table itself, and the two safety properties that matter most:

  * an account with no role behaves exactly as the app did before roles existed,
    so the change can never quietly take a feature away, and
  * only a paid psychotherapist can ever reach ICD codes.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-roles")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone
from unittest.mock import patch

import config
import roles
import TogetherMindsAI as tm
from TogetherMindsAI import app
from models import db, init_encryption, Clinician

init_encryption(TEST_KEY)

ADMIN = "raja@onlinegbc.com"


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


def _seed(uid, email, role=None, plan=None, status=None, customer=None):
    db.session.add(Clinician(
        id=uid, provider="google", provider_subject=uid, email=email,
        created_at=datetime.now(timezone.utc), role=role, plan=plan,
        subscription_status=status, stripe_customer_id=customer))
    db.session.commit()
    return uid


# ---------------------------------------------------------------------------
# The role table
# ---------------------------------------------------------------------------

def test_three_roles_exist():
    assert set(roles.ROLES) == {roles.PSYCHOTHERAPIST, roles.HYPNOTHERAPIST,
                                roles.CAREGIVER}


def test_only_a_paid_psychotherapist_reaches_icd_codes():
    """ICD coding is clinical. No other role may ever buy it."""
    assert roles.allows(roles.PSYCHOTHERAPIST, roles.ICD, paid=True) is True
    assert roles.allows(roles.PSYCHOTHERAPIST, roles.ICD, paid=False) is False
    for role in (roles.HYPNOTHERAPIST, roles.CAREGIVER):
        assert roles.allows(role, roles.ICD, paid=True) is False
        assert roles.sells(role, roles.ICD) is False    # not even for sale


def test_live_audio_video_is_free_for_every_role():
    """Watching live is the caregiver's whole free tier, and free everywhere else."""
    for role in roles.ROLES:
        assert roles.allows(role, roles.LIVE_AV, paid=False) is True


def test_recording_is_paid_for_every_role():
    for role in roles.ROLES:
        assert roles.allows(role, roles.RECORDING, paid=False) is False
        assert roles.allows(role, roles.RECORDING, paid=True) is True


def test_caregiver_has_no_words_and_so_no_ai_or_alerts():
    """Caregiver mode is a recorder. No chat means no transcript, and no
    transcript means the safety alerts have nothing to read."""
    for cap in (roles.CHAT, roles.TRANSCRIPT, roles.SAFETY, roles.AI):
        assert roles.allows(roles.CAREGIVER, cap, paid=True) is False
        assert roles.sells(roles.CAREGIVER, cap) is False


def test_safety_alerts_are_never_sold():
    """Where a role has words at all, the alerts are free — never behind a plan."""
    for role in (roles.PSYCHOTHERAPIST, roles.HYPNOTHERAPIST):
        assert roles.allows(role, roles.SAFETY, paid=False) is True
        assert roles.sells(role, roles.SAFETY) is False


def test_licence_check_is_for_clinical_work_only():
    assert roles.needs_licence_check(roles.PSYCHOTHERAPIST) is True
    assert roles.needs_licence_check(roles.HYPNOTHERAPIST) is False
    assert roles.needs_licence_check(roles.CAREGIVER) is False


def test_only_psychotherapists_may_be_described_clinically():
    assert roles.is_clinical(roles.PSYCHOTHERAPIST) is True
    assert roles.is_clinical(roles.HYPNOTHERAPIST) is False
    assert roles.is_clinical(roles.CAREGIVER) is False


# ---------------------------------------------------------------------------
# Unset / unknown roles must fail SAFE, not closed
# ---------------------------------------------------------------------------

def test_missing_role_behaves_as_it_did_before_roles_existed(client):
    """An account with no role must keep everything it had. Failing closed here
    would silently strip features from every existing user on deploy."""
    with app.app_context():
        _seed("c1", "someone@example.com", role=None)
        clin = db.session.get(Clinician, "c1")
        assert roles.role_of(clin) == roles.PSYCHOTHERAPIST


def test_unknown_role_value_falls_back_to_the_default(client):
    """A typo or a role removed in a later version must not lock anyone out."""
    with app.app_context():
        _seed("c2", "someone2@example.com", role="wizard")
        clin = db.session.get(Clinician, "c2")
        assert roles.role_of(clin) == roles.PSYCHOTHERAPIST


def test_role_of_handles_no_account_at_all():
    assert roles.role_of(None) == roles.PSYCHOTHERAPIST


# ---------------------------------------------------------------------------
# The one-time backfill
# ---------------------------------------------------------------------------

def test_backfill_makes_admins_psychotherapists_and_others_hypnotherapists(client):
    with app.app_context():
        _seed("admin", ADMIN)
        _seed("other", "someone@example.com")
        with patch.object(config, "ADMIN_EMAILS", [ADMIN]):
            tm._backfill_clinician_roles()
        assert db.session.get(Clinician, "admin").role == roles.PSYCHOTHERAPIST
        assert db.session.get(Clinician, "other").role == roles.HYPNOTHERAPIST


def test_backfill_matches_the_admin_email_despite_encryption(client):
    """Clinician.email is encrypted with a non-deterministic cipher, so the match
    has to happen in Python. Different case and spacing must still match."""
    with app.app_context():
        _seed("admin", "  RAJA@OnlineGBC.com ")
        with patch.object(config, "ADMIN_EMAILS", [ADMIN]):
            tm._backfill_clinician_roles()
        assert db.session.get(Clinician, "admin").role == roles.PSYCHOTHERAPIST


def test_backfill_clears_the_old_billing_fields(client):
    """The old Free/Pro/Premium prices are gone, so a stored plan points at a
    price that no longer exists — including test-mode ids live Stripe cannot see."""
    with app.app_context():
        _seed("paid", "paid@example.com", plan="paid", status="active",
              customer="cus_test_123")
        with patch.object(config, "ADMIN_EMAILS", [ADMIN]):
            tm._backfill_clinician_roles()
        clin = db.session.get(Clinician, "paid")
        assert clin.plan == "free"
        assert clin.subscription_status is None
        assert clin.stripe_customer_id is None
        assert clin.current_period_end is None


def test_backfill_runs_once_and_leaves_settled_accounts_alone(client):
    """It must be safe on every boot. A second run must not reset a plan that was
    bought after the first run."""
    with app.app_context():
        _seed("done", "done@example.com", role=roles.CAREGIVER,
              plan="paid", status="active", customer="cus_real")
        with patch.object(config, "ADMIN_EMAILS", [ADMIN]):
            tm._backfill_clinician_roles()
            tm._backfill_clinician_roles()
        clin = db.session.get(Clinician, "done")
        assert clin.role == roles.CAREGIVER        # untouched
        assert clin.plan == "paid"                 # NOT reset
        assert clin.stripe_customer_id == "cus_real"


def test_backfill_survives_an_account_with_no_email(client):
    """Older accounts pre-date the email column."""
    with app.app_context():
        _seed("noemail", None)
        with patch.object(config, "ADMIN_EMAILS", [ADMIN]):
            tm._backfill_clinician_roles()
        assert db.session.get(Clinician, "noemail").role == roles.HYPNOTHERAPIST
