"""
tests/test_android_package.py
-----------------------------
The Android (Trusted Web Activity) package must be FamilyCareCam, not the old
TogetherMindsAI app: same package ID, host, name and version in every file, and
the site's assetlinks.json must name the new package and must NOT carry the old
app's signing fingerprints.
"""
import json
import os
import re
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-android")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")
os.environ.setdefault("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())

from TogetherMindsAI import app

ROOT = os.path.dirname(os.path.dirname(__file__))
PKG = "com.onlinegbc.familycarecam"
HOST = "familycarecam.onlinegbc.com"
NEW_FINGERPRINT = (
    "87:F0:7F:B0:F9:F2:9C:33:31:ED:F5:ED:57:37:48:79:76:E5:BB:69:C7:01:34:89:59:50:4F:1F:EF:CB:7E:21")
OLD = ("togethermindsai", "tm.onlinegbc.com", "TMAI", "TogetherMinds")


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_gradle_uses_new_package_host_name_and_version():
    g = _read("app", "build.gradle")
    assert f"applicationId: '{PKG}'" in g
    assert f'applicationId "{PKG}"' in g
    assert f'namespace "{PKG}"' in g
    assert f"hostName: '{HOST}'" in g
    assert "name: 'FamilyCareCam'" in g
    assert "versionCode 1" in g and 'versionName "1"' in g
    for old in OLD:
        assert old not in g


def test_manifest_strings_and_web_manifest_have_no_old_name():
    for parts in (("app", "src", "main", "AndroidManifest.xml"),
                  ("app", "src", "main", "res", "values", "strings.xml"),
                  ("app", "src", "main", "res", "raw", "web_app_manifest.json")):
        text = _read(*parts)
        for old in OLD:
            assert old not in text, (parts, old)
    assert f'package="{PKG}"' in _read("app", "src", "main", "AndroidManifest.xml")
    assert HOST in _read("app", "src", "main", "res", "values", "strings.xml")


def test_java_sources_live_in_the_new_package_folder():
    base = os.path.join(ROOT, "app", "src", "main", "java", "com", "onlinegbc")
    assert not os.path.isdir(os.path.join(base, "togethermindsai"))
    folder = os.path.join(base, "familycarecam")
    files = [f for f in os.listdir(folder) if f.endswith(".java")]
    assert files
    for f in files:
        with open(os.path.join(folder, f), encoding="utf-8") as fh:
            assert re.search(rf"^package {re.escape(PKG)};", fh.read(), re.M), f


def test_twa_manifest_matches_gradle():
    t = json.loads(_read("twa-manifest.json"))
    assert t["packageId"] == PKG
    assert t["host"] == HOST
    assert t["name"] == "FamilyCareCam" and t["launcherName"] == "FamilyCareCam"
    assert t["signingKey"]["alias"] == "familycarecam"
    assert t["signingKey"]["path"].endswith("familycarecam.keystore")
    assert "android.keystore" not in t["signingKey"]["path"]
    assert t["appVersionCode"] == 1 and t["appVersionName"] == "1"
    for key in ("iconUrl", "maskableIconUrl", "webManifestUrl", "fullScopeUrl"):
        assert t[key].startswith(f"https://{HOST}/"), key


def test_assetlinks_names_new_package_and_no_old_fingerprints(client):
    r = client.get("/.well-known/assetlinks.json")
    assert r.status_code == 200
    data = json.loads(r.data)
    target = data[0]["target"]
    assert target["package_name"] == PKG
    assert "togethermindsai" not in r.data.decode()
    assert target["sha256_cert_fingerprints"] == [NEW_FINGERPRINT]
