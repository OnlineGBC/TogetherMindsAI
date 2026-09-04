"""Tests for cloudbuild.yaml configuration constraints."""
import re
import pathlib

CLOUDBUILD = pathlib.Path(__file__).parent.parent / "cloudbuild.yaml"


def test_max_instances_is_one():
    """max-instances must be 1 so all Socket.IO sessions share one instance.

    Socket.IO rooms are in-memory. If Cloud Run scales beyond 1 instance,
    users in the same couple/group session can land on different instances
    and never receive each other's messages (POST 400 errors).
    """
    text = CLOUDBUILD.read_text()
    # Find the value that follows --max-instances
    match = re.search(r"--max-instances\s*\n\s*-\s*\"?(\d+)\"?", text)
    assert match, "--max-instances not found in cloudbuild.yaml"
    assert match.group(1) == "1", (
        f"max-instances must be 1 to prevent Socket.IO cross-instance failures, got {match.group(1)}"
    )


def test_the_ehr_launch_has_everything_it_needs():
    """EHR_ENABLED on its own is not enough — the routes need the client id and
    the sandbox secret too.

    Without this, a deploy could switch the launch ON while leaving it
    unconfigured. /ehr/launch would then 503 for every clinician, and the reason
    would be a single log line rather than anything visible.
    """
    text = CLOUDBUILD.read_text()
    # Read the ARGUMENT, not the file. A first version of this searched the whole
    # text and passed on the explanatory COMMENT above the flag — so deleting the
    # flag itself went unnoticed.
    match = re.search(r"--set-env-vars\s*\n\s*-\s*(\S+)", text)
    assert match, "--set-env-vars not found in cloudbuild.yaml"
    env_pairs = dict(p.split("=", 1) for p in match.group(1).split(",") if "=" in p)
    assert env_pairs.get("EHR_ENABLED") == "true", env_pairs
    assert "EPIC_CLIENT_ID=EPIC_CLIENT_ID:" in text
    assert "EPIC_SANDBOX_CLIENT_SECRET=EPIC_SANDBOX_CLIENT_SECRET:" in text


def test_the_secrets_the_app_reads_are_all_wired():
    """Every config value read from the environment for a FEATURE THAT IS ON must
    appear in the deploy args. A missing one fails silently — the app boots, the
    feature is just quietly dead."""
    text = CLOUDBUILD.read_text()
    for name in ("SECRET_KEY", "DATABASE_URL", "FIELD_ENCRYPTION_KEY",
                 "ANTHROPIC_API_KEY", "LIVEKIT_API_KEY", "ASSEMBLYAI_API_KEY",
                 "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
                 "ADMIN_EMAILS", "EPIC_CLIENT_ID"):
        assert name + "=" in text, name
