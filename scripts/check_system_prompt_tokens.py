"""
check_system_prompt_tokens.py
-----------------------------
Verify the cached system prompt in ai_therapist.py is large enough to
actually benefit from prompt caching on Claude Opus 4.7.

Why this matters
================
Opus 4.7's minimum cacheable prefix is 4096 tokens (Sonnet 4.6 was 2048).
If the rendered system prompt is below the threshold, `cache_control:
{"type": "ephemeral"}` markers silently no-op — no error, just full-price
input tokens on every request, forever.

What this script does
=====================
Renders _SYSTEM_PROMPT_TEMPLATE for each session mode (solo / couple / group)
exactly as generate_response() / generate_opening_message() / generate_silence_nudge()
do, calls client.messages.count_tokens() against claude-opus-4-7, and reports
whether each rendered prompt clears the 4096-token cacheable threshold.

Usage
-----
    python scripts/check_system_prompt_tokens.py

Requires ANTHROPIC_API_KEY in the environment (same as running the app).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env the same way the app does so ANTHROPIC_API_KEY is available
from dotenv import load_dotenv
load_dotenv()

from ai_therapist import (
    _SYSTEM_PROMPT_TEMPLATE,
    _MODE_CONTEXT,
    _get_claude_client,
)

OPUS_47_CACHE_MIN = 4096
MODEL = "claude-opus-4-7"


def render_prompt(mode: str) -> str:
    """Render the system prompt exactly as the live code paths do."""
    return _SYSTEM_PROMPT_TEMPLATE.format(
        mode=mode,
        mode_context=_MODE_CONTEXT.get(mode, _MODE_CONTEXT["solo"]),
    )


def count_tokens_for_mode(client, mode: str) -> tuple[int, int]:
    """Return (system_tokens, total_tokens_with_minimal_user) for one mode.

    We send the exact system block shape the production code uses (with the
    cache_control marker — it has no effect on token count but matches the
    real request bytes). The user turn is a single "hi" so total_tokens is
    effectively system_tokens + a small constant.
    """
    system_block = [{
        "type": "text",
        "text": render_prompt(mode),
        "cache_control": {"type": "ephemeral"},
    }]
    result = client.messages.count_tokens(
        model=MODEL,
        system=system_block,
        messages=[{"role": "user", "content": "hi"}],
    )
    total = result.input_tokens

    # Subtract the user-turn delta by counting an empty-system version
    empty_system = client.messages.count_tokens(
        model=MODEL,
        messages=[{"role": "user", "content": "hi"}],
    )
    system_only = total - empty_system.input_tokens
    return system_only, total


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in environment.")
        return 1

    client = _get_claude_client()
    print(f"Counting tokens against {MODEL}")
    print(f"Cacheable-prefix threshold: {OPUS_47_CACHE_MIN} tokens")
    print("-" * 70)

    all_pass = True
    for mode in ("solo", "couple", "group"):
        system_tokens, total_with_user = count_tokens_for_mode(client, mode)
        passes = system_tokens >= OPUS_47_CACHE_MIN
        status = "OK  (will cache)" if passes else "FAIL (silently won't cache)"
        margin = system_tokens - OPUS_47_CACHE_MIN
        sign = "+" if margin >= 0 else ""
        print(
            f"{mode:6s}  system={system_tokens:5d} tokens  "
            f"(margin {sign}{margin})  -> {status}"
        )
        if not passes:
            all_pass = False

    print("-" * 70)
    if all_pass:
        print("All modes clear the 4096-token threshold. Prompt caching is active.")
        return 0
    else:
        print(
            "At least one mode is below 4096 tokens. cache_control breakpoints\n"
            "on those modes silently no-op. Options:\n"
            "  1. Expand the system prompt (e.g. add few-shot examples) above 4096.\n"
            "  2. Remove cache_control on affected sites (clarity over false hope).\n"
            "  3. Accept the cost and move on (system prompt is small enough that\n"
            "     the cache savings would be modest anyway)."
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
