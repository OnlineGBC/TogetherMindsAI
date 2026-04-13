"""
session_id.py — Single source of truth for all session ID logic.

Generation, display transformation, validation, and user-facing format hints
all live here. Nothing else in the codebase should encode assumptions about
session ID formats — derive everything from these functions and constants.

Mode rules:
  solo   — internal ID is the creator's UUID (36 chars with hyphens).
            Display ID is the first DISPLAY_ID_LENGTH chars after stripping hyphens,
            uppercased (e.g. "7a6d1ebd-..." → "7A6D1E").
  couple — same as solo.
  group  — internal ID is a random GROUP_ID_LENGTH-char uppercase alphanumeric
            string drawn from GROUP_CHARSET (no I/O/0/1 to avoid ambiguity).
            Display ID equals the internal ID (already short and readable).
"""

import secrets

# ---------------------------------------------------------------------------
# Constants — all format decisions live here
# ---------------------------------------------------------------------------

# Unambiguous charset for group codes: no 0/O (zero/oh), no 1/I/l (one/eye/ell)
GROUP_CHARSET: str = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
GROUP_ID_LENGTH: int = 6

# Solo/couple display IDs: first N chars of the UUID with hyphens stripped
DISPLAY_ID_LENGTH: int = 6

# Modes that use a UUID as their internal session ID
_UUID_MODES = {"solo", "couple"}
# Modes that use a generated short code as their internal session ID
_CODE_MODES = {"group"}
_ALL_MODES = _UUID_MODES | _CODE_MODES


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate_group_session_id(existing_ids: set | None = None) -> str:
    """Generate a new unique group session ID.

    Uses secrets.choice (CSPRNG) for each character. With GROUP_CHARSET of 32
    characters and GROUP_ID_LENGTH of 6, there are 32^6 ≈ 1 billion possible
    IDs, making collisions negligible at current scale.

    Args:
        existing_ids: Optional set of already-in-use IDs. If provided, will
                      retry up to 5 times to avoid a collision. Pass None to
                      skip the check (acceptable at small scale).

    Returns:
        A GROUP_ID_LENGTH-character uppercase string from GROUP_CHARSET.

    Raises:
        RuntimeError: If a unique ID cannot be generated after 5 attempts.
    """
    for _ in range(5):
        candidate = "".join(secrets.choice(GROUP_CHARSET) for _ in range(GROUP_ID_LENGTH))
        if existing_ids is None or candidate not in existing_ids:
            return candidate
    raise RuntimeError(
        f"Could not generate a unique group session ID after 5 attempts. "
        f"existing_ids size: {len(existing_ids) if existing_ids else 0}"
    )


# ---------------------------------------------------------------------------
# Display transformation
# ---------------------------------------------------------------------------

def to_display_id(session_id: str, mode: str) -> str:
    """Convert a raw session ID to its user-facing display form.

    This is the single enforced path for display ID derivation. Templates must
    only show the return value of this function — never a raw UUID or the
    session_id variable directly in user-facing text.

    Args:
        session_id: The raw session ID as stored in the database.
        mode: The session mode — one of "solo", "couple", "group".

    Returns:
        A short, readable display ID appropriate for the mode.

    Raises:
        ValueError: If mode is not a recognised session mode.
    """
    if mode not in _ALL_MODES:
        raise ValueError(
            f"Unknown session mode {mode!r}. Expected one of: {sorted(_ALL_MODES)}"
        )
    if mode in _UUID_MODES:
        # Strip hyphens, uppercase, take first DISPLAY_ID_LENGTH chars
        return session_id.replace("-", "").upper()[:DISPLAY_ID_LENGTH]
    # Group: the internal ID is already the display ID
    return session_id


# ---------------------------------------------------------------------------
# Validation / normalisation
# ---------------------------------------------------------------------------

def is_valid_group_id(value: str) -> bool:
    """Return True if value looks like a well-formed group session ID.

    Checks exact length and that every character is in GROUP_CHARSET.
    Note: GROUP_CHARSET is all uppercase, so lowercase inputs return False.
    Use value.upper() before calling if you want case-insensitive matching.

    Args:
        value: The string to test.

    Returns:
        True if value matches the group ID format, False otherwise.
    """
    if len(value) != GROUP_ID_LENGTH:
        return False
    return all(c in GROUP_CHARSET for c in value)


def normalise_join_input(raw: str) -> str:
    """Normalise a user-submitted session ID or nickname for lookup.

    Group IDs are stored uppercase. If the input matches the group ID pattern
    when uppercased, return the uppercased form so the DB lookup succeeds.
    UUIDs and nicknames are returned unchanged (UUID letters are lowercase in
    the DB; nicknames use case-insensitive comparison in the query).

    Args:
        raw: The stripped user input from the join form.

    Returns:
        The normalised string for use in DB lookups.
    """
    if is_valid_group_id(raw.upper()):
        return raw.upper()
    return raw


# ---------------------------------------------------------------------------
# User-facing format hints (for join_session.html)
# ---------------------------------------------------------------------------

def rejoin_format_hint(mode: str | None = None) -> str:
    """Return the human-readable help text describing what to enter on the join page.

    Built from module constants, so it stays in sync with the actual formats
    automatically. Templates should render this as {{ rejoin_hint }} rather
    than embedding hardcoded descriptions.

    Args:
        mode: Restrict the hint to a specific mode, or None for all modes.

    Returns:
        A descriptive sentence string.
    """
    group_desc = (
        f"For group sessions enter the {GROUP_ID_LENGTH}-character code "
        f"(e.g. {_example_group_id()})."
    )
    uuid_desc = (
        f"For solo or couples sessions enter the {DISPLAY_ID_LENGTH}-character "
        f"short code shown in your session header, or the full session UUID."
    )

    if mode == "group":
        return group_desc
    if mode in _UUID_MODES:
        return uuid_desc
    # None or unrecognised — return combined hint for all modes
    return f"{group_desc} {uuid_desc}"


def rejoin_placeholder(mode: str | None = None) -> str:
    """Return an appropriate placeholder string for the session ID input field.

    Args:
        mode: Restrict to a specific mode, or None for the general join page.

    Returns:
        A placeholder string.
    """
    if mode == "group":
        return f"e.g. {_example_group_id()}"
    if mode in _UUID_MODES:
        return "e.g. a full UUID or 6-char short code"
    # General join page — most helpful example includes nickname, group code, UUID
    return f"e.g. My Monday session, {_example_group_id()}, or a full UUID"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _example_group_id() -> str:
    """Return a static illustrative example of a group ID (not a real session)."""
    # Hard-coded so the placeholder is deterministic and readable.
    # It uses only characters from GROUP_CHARSET to be self-documenting.
    return "AB3K7M"
