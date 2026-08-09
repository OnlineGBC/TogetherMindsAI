"""Tests for .gitignore / .gcloudignore constraints around signing credentials.

The Android signing keystore is the credential that proves an app update comes
from us. Committing it (or shipping it in a build context) hands that over. The
ignore rules used to name one exact file, so a copy under any other name -- an
old-password backup, a .jks from a different tool -- sat untracked and one
`git add -A` away from being committed.
"""
import pathlib

ROOT = pathlib.Path(__file__).parent.parent
GITIGNORE = ROOT / ".gitignore"
GCLOUDIGNORE = ROOT / ".gcloudignore"

# Suffixes any Android/Java signing credential turns up under.
KEYSTORE_PATTERNS = ("*.keystore", "*.keystore.*", "*.jks", "*.p12", "*.pepk")


def test_gitignore_covers_keystore_patterns():
    lines = {ln.strip() for ln in GITIGNORE.read_text().splitlines()}
    missing = [p for p in KEYSTORE_PATTERNS if p not in lines]
    assert not missing, (
        f".gitignore must cover signing-credential patterns, missing: {missing}. "
        "Naming one exact file leaves renamed copies committable."
    )


def test_no_signing_credential_is_tracked_or_present_unignored():
    """Nothing matching a keystore suffix should be sitting in the repo tracked."""
    import subprocess
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    offenders = [f for f in tracked
                 if f.endswith((".keystore", ".jks", ".p12", ".pepk"))
                 or ".keystore." in f]
    assert not offenders, f"signing credential committed to the repo: {offenders}"


def test_gcloudignore_covers_keystore_patterns():
    """The build context is uploaded whole to Cloud Build, so the same pattern cover
    the repo has must apply here — a renamed keystore is the same credential."""
    text = GCLOUDIGNORE.read_text(encoding="utf-8", errors="replace")
    lines = {ln.strip() for ln in text.splitlines()}
    missing = [p for p in KEYSTORE_PATTERNS if p not in lines]
    assert not missing, (
        f".gcloudignore must cover signing-credential patterns, missing: {missing}"
    )
