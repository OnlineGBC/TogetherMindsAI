"""
tests/test_cam_diag.py
----------------------
The temporary camera-shape log (/rtc/cam-diag) used to trace a sideways or
upside-down picture on a phone held in landscape. It must only accept admitted
participants, log sizes and orientation only, and strip anything odd.
"""
import logging
import os
import pathlib
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from tests.test_rtc_authz import enc_client, _seed, _login, _rtc_on  # noqa: F401
from TogetherMindsAI import app

PAGE = (pathlib.Path(__file__).resolve().parent.parent / "templates" / "session_live.html").read_text(encoding="utf-8")


def test_cam_diag_refuses_a_stranger(enc_client):
    sid = _seed()
    _login(enc_client, "stranger")
    with _rtc_on():
        rv = enc_client.post("/rtc/cam-diag", json={"session_id": sid, "track_w": 480})
    assert rv.status_code == 403


def test_cam_diag_logs_sizes_and_orientation_and_strips_other_characters(enc_client, caplog):
    sid = _seed()
    _login(enc_client, "ther-1")
    with _rtc_on(), caplog.at_level(logging.INFO, logger=app.logger.name):
        rv = enc_client.post("/rtc/cam-diag", json={
            "session_id": sid, "tile": "local", "role": "watched",
            "track_w": 480, "track_h": 640, "video_w": 480, "video_h": 640,
            "orient": "landscape-primary", "angle": 90,
            "facing": "user\nFAKE LOG LINE", "extra": "ignored"})
    assert rv.status_code == 200
    line = next(r.getMessage() for r in caplog.records if "CAM-DIAG" in r.getMessage())
    assert "track_w=480" in line and "track_h=640" in line
    assert "orient=landscape-primary" in line and "angle=90" in line
    assert "\n" not in line and "extra" not in line


def test_the_page_reports_each_video_picture():
    assert "function camDiag(" in PAGE
    assert "camDiag(id, mediaEl);" in PAGE
    assert '"/rtc/cam-diag"' in PAGE


def test_cam_diag_is_logged_at_a_level_the_server_keeps(enc_client, caplog):
    """The app's logger drops INFO in production, which hid the first test run."""
    sid = _seed()
    _login(enc_client, "ther-1")
    with _rtc_on(), caplog.at_level(logging.WARNING, logger=app.logger.name):
        enc_client.post("/rtc/cam-diag", json={"session_id": sid, "track_w": 480})
    assert any("CAM-DIAG" in r.getMessage() and r.levelno >= logging.WARNING for r in caplog.records)
