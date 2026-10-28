"""
tests/test_tilt_diag.py
-----------------------
Temporary test report (/rtc/tilt-diag): proves the patient's page can read the
phone's tilt sensor inside the TMAI app, before the picture-turning fix is built.
Only admitted participants may send it; only numbers are logged; and only the
watched person in a caregiver session sends it.
"""
import logging
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import roles
from tests.test_rtc_authz import enc_client, _seed, _login, _rtc_on  # noqa: F401
from tests.test_caregiver_room import client, _room, _watched_room  # noqa: F401
from TogetherMindsAI import app

MARK = '"/rtc/tilt-diag"'


def test_tilt_diag_refuses_a_stranger(enc_client):
    sid = _seed()
    _login(enc_client, "stranger")
    with _rtc_on():
        rv = enc_client.post("/rtc/tilt-diag", json={"session_id": sid, "event": "upright"})
    assert rv.status_code == 403


def test_tilt_diag_logs_numbers_at_a_level_the_server_keeps(enc_client, caplog):
    sid = _seed()
    _login(enc_client, "ther-1")
    with _rtc_on(), caplog.at_level(logging.WARNING, logger=app.logger.name):
        rv = enc_client.post("/rtc/tilt-diag", json={
            "session_id": sid, "event": "side-a", "gx": "9.6", "gy": "0.4", "gz": "1.1",
            "orient": "portrait-primary", "angle": 0, "beta": "x\nFAKE LINE", "extra": "no"})
    assert rv.status_code == 200
    rec = next(r for r in caplog.records if "TILT-DIAG" in r.getMessage())
    line = rec.getMessage()
    assert rec.levelno >= logging.WARNING
    assert "event=side-a" in line and "gx=9.6" in line and "orient=portrait-primary" in line
    assert "\n" not in line and "extra" not in line


def test_the_watched_person_in_a_caregiver_session_sends_it(client):
    assert MARK in _watched_room(client, roles.CAREGIVER)


def test_the_caregiver_does_not_send_it(client):
    assert MARK not in _room(client, roles.CAREGIVER)


def test_a_therapy_client_does_not_send_it(client):
    # One role per test: the room helpers reuse the same ids, and the database
    # session would otherwise hand back the previous test's clinician.
    assert MARK not in _watched_room(client, roles.PSYCHOTHERAPIST)


def test_a_therapist_does_not_send_it(client):
    assert MARK not in _room(client, roles.PSYCHOTHERAPIST)
