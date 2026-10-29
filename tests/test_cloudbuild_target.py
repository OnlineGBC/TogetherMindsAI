"""
tests/test_cloudbuild_target.py
-------------------------------
Which Cloud Run app does `gcloud builds submit --config cloudbuild.yaml` deploy?

On this branch (main-therapist-conf) the answer must be TogetherMindsAI (service
`togethermindsai`), with its own settings. The BabyCamSecure app deploys from the
babycamsecure branch. The check reads the file as text (PyYAML is not a dependency).
It exists because the deploy line `--set-env-vars` REPLACES every setting on the
service, and because a default that points at the other app would send this
branch's code to it.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(__file__))


def _file():
    with open(os.path.join(ROOT, "cloudbuild.yaml"), encoding="utf-8") as f:
        return f.read()


def _env_line(src):
    """The value line that follows `--set-env-vars` (comment lines may sit between)."""
    lines = src.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.strip() == "- --set-env-vars")
    for ln in lines[i + 1:]:
        if ln.strip() and not ln.strip().startswith("#"):
            return ln.strip().lstrip("- ").strip()
    raise AssertionError("no value after --set-env-vars")


def test_the_default_target_is_togethermindsai():
    src = _file()
    assert re.search(r"^  _SERVICE_NAME: togethermindsai$", src, re.M)
    assert re.search(r"^  _TAG: latest$", src, re.M)
    assert not re.search(r"^  _SERVICE_NAME: babycamsecure$", src, re.M)


def test_it_uses_the_shared_project_resources():
    src = _file()
    assert re.search(r"^  _AR_REPO: togethermindsai-repo$", src, re.M)
    assert re.search(r"^  _SQL_CONN_NAME: togethermindsai-python:us-central1:togethermindsai-db$",
                     src, re.M)


def test_the_settings_line_keeps_ehr_on_and_names_its_own_address():
    env = dict(kv.split("=", 1) for kv in _env_line(_file()).split(","))
    assert env["EHR_ENABLED"] == "true"
    assert env["EHR_WRITE_ENABLED"] == "true"
    assert env["BILLING_ENABLED"] == "true"
    assert env["RECORDING_ENABLED"] == "true"
    assert env["PUBLIC_BASE_URL"] == "https://tm.onlinegbc.com"


def test_the_file_keeps_the_always_on_single_instance_settings():
    src = _file()
    assert re.search(r"- --min-instances\s+- \"1\"", src)
    assert re.search(r"- --max-instances\s+- \"1\"", src)
    assert "--allow-unauthenticated" in src


def test_the_header_says_which_app_this_file_deploys():
    head = "\n".join(_file().splitlines()[:8])
    assert "Cloud Run service: togethermindsai" in head
    assert "babycamsecure branch" in head
