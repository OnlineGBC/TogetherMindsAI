"""
session_id.py — Single source of truth for all session ID logic.

All sessions (solo, couple, group) use the same 16-character mixed-case
ID drawn from SESSION_CHARSET. The ID stored in the DB is exactly what is
shown to the user — no derivation, no transformation.

The session_id is effectively a shared password — anyone holding it can
join the session — so the format is sized to be unguessable:
59 characters × 16 slots ≈ 94 bits of entropy, comfortably past NIST's
80-bit threshold.

Lookups are case-insensitive: 'aB3k7M-X9_pQrT!$' and 'AB3K7M-X9_PQRT!$'
find the same session. Ambiguous characters are excluded to prevent
misreading:
  - uppercase: no I (eye), no O (oh)
  - lowercase: no i (eye), no l (ell), no o (oh)
  - digits:    no 0 (zero), no 1 (one)
  - symbols:   only `-` `_` `!` `$` — URL-path-safe per RFC 3986, on every
               keyboard, visually distinct, and free of chat-app auto-link
               edge cases. `#`, `%`, `&`, `@`, `^` etc. are excluded because
               they either break URL routing or get percent-encoded.
"""

import secrets

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 24 uppercase + 23 lowercase + 8 digits + 4 symbols = 59 characters,
# case-sensitive. `-` `_` are RFC 3986 unreserved; `!` `$` are sub-delims
# allowed unencoded in URL path segments.
SESSION_CHARSET: str = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789-_!$"
SESSION_ID_LENGTH: int = 16


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate_session_id(existing_ids: set | None = None) -> str:
    """Generate a new unique session ID for any session mode.

    Uses secrets.choice (CSPRNG). With 59 characters and length 16 there are
    59^16 ≈ 1.5 × 10^28 possible IDs, making collisions astronomically unlikely.

    Args:
        existing_ids: Optional set of already-in-use IDs. If provided, retries
                      up to 5 times to avoid a collision. Pass None to skip
                      the check (acceptable at small scale).

    Returns:
        A SESSION_ID_LENGTH-character string from SESSION_CHARSET.

    Raises:
        RuntimeError: If a unique ID cannot be generated after 5 attempts.
    """
    for _ in range(5):
        candidate = "".join(secrets.choice(SESSION_CHARSET) for _ in range(SESSION_ID_LENGTH))
        if existing_ids is None or candidate not in existing_ids:
            return candidate
    raise RuntimeError(
        f"Could not generate a unique session ID after 5 attempts "
        f"(existing_ids size: {len(existing_ids) if existing_ids else 0})"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def is_valid_session_id(value: str) -> bool:
    """Return True if value is a well-formed session ID.

    Checks exact length and that every character is in SESSION_CHARSET.
    Validation is case-sensitive (checks the stored form); lookups are case-insensitive.

    Args:
        value: The string to test.

    Returns:
        True if value matches the session ID format, False otherwise.
    """
    if len(value) != SESSION_ID_LENGTH:
        return False
    return all(c in SESSION_CHARSET for c in value)


# ---------------------------------------------------------------------------
# Rejoin lookup
# ---------------------------------------------------------------------------

def normalise_join_input(raw: str) -> str:
    """Normalise a user-submitted session ID for lookup.

    Session IDs are case-insensitive for lookup: the input is uppercased so
    a user who types 'ab3k7m-x9_pqrt!$' matches a session stored as 'aB3k7M-X9_pQrT!$'.
    Whitespace is stripped first.

    Args:
        raw: The user input from the join form.

    Returns:
        The stripped, uppercased input.
    """
    return raw.strip().upper()


# ---------------------------------------------------------------------------
# User-facing format hints (for join_session.html)
# ---------------------------------------------------------------------------

def rejoin_format_hint() -> str:
    """Return the human-readable help text for the join page.

    Built from module constants so it stays in sync automatically.

    Returns:
        A descriptive sentence string.
    """
    return (
        f"Enter the {SESSION_ID_LENGTH}-character Session ID shown in your session header "
        f"(e.g. {_example_session_id()}). "
        f"Session IDs are not case-sensitive — you can type in any mix of upper and lower case."
    )


def rejoin_placeholder() -> str:
    """Return the placeholder string for the session ID input field.

    Returns:
        A placeholder string.
    """
    return f"e.g. My Monday session or {_example_session_id()}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _example_session_id() -> str:
    """Return a static illustrative example of a session ID (not a real session)."""
    # Hard-coded for determinism and readability.
    # Uses only characters from SESSION_CHARSET to self-document the format.
    return "aB3k7M-X9_pQrT!$"
