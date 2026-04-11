import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import time
import pytest

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-rate-limit")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

from app import app, _check_rate_limit, _RATE_MAX_MSGS, _RATE_WINDOW
from models import db, RateLimitEntry


@pytest.fixture
def test_ctx():
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def test_rate_limit_allows_within_window(test_ctx):
    uid = "rate-test-user-1"
    for _ in range(_RATE_MAX_MSGS - 1):
        assert _check_rate_limit(uid) is True


def test_rate_limit_blocks_at_threshold(test_ctx):
    uid = "rate-test-user-2"
    for _ in range(_RATE_MAX_MSGS):
        _check_rate_limit(uid)
    assert _check_rate_limit(uid) is False


def test_rate_limit_resets_after_window(test_ctx):
    uid = "rate-test-user-3"
    for _ in range(_RATE_MAX_MSGS):
        _check_rate_limit(uid)
    assert _check_rate_limit(uid) is False

    # Manually expire all entries past the window
    past = time.time() - _RATE_WINDOW - 1
    RateLimitEntry.query.filter_by(user_id=uid).update({"timestamp": past})
    db.session.commit()

    assert _check_rate_limit(uid) is True


def test_rate_limit_independent_per_user(test_ctx):
    uid_a = "rate-test-user-a"
    uid_b = "rate-test-user-b"
    for _ in range(_RATE_MAX_MSGS):
        _check_rate_limit(uid_a)
    # uid_a is blocked, uid_b should still be allowed
    assert _check_rate_limit(uid_a) is False
    assert _check_rate_limit(uid_b) is True
