"""
tests/test_feedback_email.py
----------------------------
Unit tests for feedback_email.py — the pure feedback-email builder extracted
from the app monolith. No app, no DB, no SMTP.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import feedback_email


def test_labels():
    assert feedback_email.device_label("android_twa", None) == "Android (installed app)"
    assert feedback_email.device_label("web", "macos") == "Mac laptop / desktop"
    assert feedback_email.mode_label("couple") == "Couple Check-in"
    assert feedback_email.mode_label(None) == "Not in a session"
    assert feedback_email.pay_label("yes") == "Yes"
    assert feedback_email.stars(3) == "★★★☆☆"
    assert feedback_email.stars(None) == "Not rated"


def test_format_feedback_email_full():
    subject, plain, html = feedback_email.format_feedback_email({
        "rating": 4, "would_pay": "maybe", "platform": "web", "os": "windows",
        "mode": "group", "what_worked": "The video was smooth",
        "what_to_improve": "", "desired_features": "Dark mode", "other": "",
    })
    assert "Group Circle" in subject and "4 / 5" in subject
    assert "The video was smooth" in plain and "Dark mode" in plain
    assert html.startswith("<!DOCTYPE html>")
    assert "The video was smooth" in html and "Dark mode" in html


def test_format_feedback_email_escapes_html():
    """User text is HTML-escaped — no injection into the clinician's inbox."""
    _s, _p, html = feedback_email.format_feedback_email({
        "rating": 5, "what_worked": "<script>alert(1)</script>",
    })
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_format_feedback_email_empty_payload():
    subject, plain, html = feedback_email.format_feedback_email({})
    assert subject and plain and html          # renders without raising
    assert "N/A" in subject                     # no rating → N/A
    assert "(none)" in plain                     # empty sections labelled
