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
