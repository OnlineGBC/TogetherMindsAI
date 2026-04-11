import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import uuid
import pytest

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-input")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

from app import app, _MAX_MSG_LEN
from models import db, User


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["user_id"] = user_id
            yield c, user_id
        db.session.remove()
        db.drop_all()


def test_message_too_long_returns_redirect(client):
    c, uid = client
    long_msg = "a" * (_MAX_MSG_LEN + 1)
    rv = c.post(f"/therapy/solo/{uid}", data={"message": long_msg})
    assert rv.status_code == 302


def test_message_empty_returns_redirect(client):
    c, uid = client
    rv = c.post(f"/therapy/solo/{uid}", data={"message": ""})
    assert rv.status_code == 302


def test_message_whitespace_only_returns_redirect(client):
    c, uid = client
    rv = c.post(f"/therapy/solo/{uid}", data={"message": "   "})
    assert rv.status_code == 302


def test_message_at_max_length_is_accepted(client):
    c, uid = client
    max_msg = "a" * _MAX_MSG_LEN
    rv = c.post(f"/therapy/solo/{uid}", data={"message": max_msg})
    assert rv.status_code == 302   # redirect back to solo page after save


def test_message_valid_saves_and_redirects(client):
    c, uid = client
    rv = c.post(f"/therapy/solo/{uid}", data={"message": "Hello, I need some support today."})
    assert rv.status_code == 302
