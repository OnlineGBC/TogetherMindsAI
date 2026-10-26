"""
tests/test_orientation_unlocked.py
----------------------------------
The app must turn with the phone. While it was locked to portrait, a phone held in
landscape sent a tall camera picture with the person turned sideways or upside
down, and the caregiver saw it in a narrow strip (logged by /rtc/cam-diag).
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_web_manifest_does_not_lock_orientation():
    m = json.loads((ROOT / "static" / "manifest.json").read_text(encoding="utf-8"))
    assert m["orientation"] == "any"


def test_the_android_wrapper_follows_the_phone():
    t = json.loads((ROOT / "twa-manifest.json").read_text(encoding="utf-8"))
    assert t["orientation"] == "default"
    g = (ROOT / "app" / "build.gradle").read_text(encoding="utf-8")
    assert re.search(r"orientation:\s*'default'", g)
    assert "portrait" not in re.search(r"orientation:[^\n]*", g).group(0)
