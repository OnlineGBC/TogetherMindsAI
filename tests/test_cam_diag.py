"""
tests/test_cam_diag.py
----------------------
The temporary camera-shape log (/rtc/cam-diag) traced the upside-down picture on
a phone held sideways. That is fixed (mirrored pictures), so the log is removed:
no page sends it and the server no longer accepts it.
"""
import os
import pathlib
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from tests.test_rtc_authz import enc_client, _seed, _login, _rtc_on  # noqa: F401

PAGE = (pathlib.Path(__file__).resolve().parent.parent / "templates" / "session_live.html").read_text(encoding="utf-8")


def test_the_page_no_longer_reports_video_pictures():
    assert "camDiag" not in PAGE
    assert "/rtc/cam-diag" not in PAGE


def test_the_server_no_longer_has_the_camera_log(enc_client):
    sid = _seed()
    _login(enc_client, "ther-1")
    with _rtc_on():
        rv = enc_client.post("/rtc/cam-diag", json={"session_id": sid, "track_w": 480})
    assert rv.status_code == 404
