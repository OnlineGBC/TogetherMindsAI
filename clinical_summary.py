"""
clinical_summary.py
-------------------
Therapist-only, on-demand summary of a session for the licensed clinician.

Produces three parts, all private to the therapist (the client never sees them
through the system):

  1. clinical        — a detailed clinical recap for the therapist.
  2. codes_rationale — a short note on which of the ICD codes that ALREADY
                       surfaced in-session best fit, framed as decision support
                       for billing/coding — never a diagnosis or a determination.
  3. client_recap    — a warm, plain-language recap the therapist MAY choose to
                       hand to the client; contains no codes. The system never
                       delivers it — handover is entirely the therapist's choice.

Pure module: one Claude call, and it never raises — any API or parse failure
returns None so the caller (console request or transcript download) always
completes. The surfaced codes themselves are deterministic (read from the corpus
via the persisted cards), so they are shown even when this narrative is absent.
"""

import json
import logging

from ai_therapist import _get_claude_client

logger = logging.getLogger(__name__)

__all__ = ["generate", "DISCLAIMER"]

# Opus for the clinical summary: an infrequent, on-demand, quality-sensitive call
# (richer clinical synthesis + tighter grounding than Sonnet). Drop to
# "claude-sonnet-4-6" to trade some quality for lower cost/latency.
SUMMARY_MODEL = "claude-opus-4-8"

DISCLAIMER = (
    "Therapist-only. AI-generated decision support — not a diagnosis and not a "
    "billing or coding determination. The clinician's professional judgment governs."
)

_SYSTEM_PROMPT = """\
You are a clinical documentation assistant writing a PRIVATE, therapist-only summary \
of a single therapy session. Everything you produce is seen ONLY by the licensed \
clinician — never by the client — unless the clinician personally chooses to share it.

Return a JSON object with exactly these three string fields:

  "clinical": a detailed clinical recap FOR THE THERAPIST — the presenting concern,
      the key themes, the emotional arc of the session, any risk or safety signals,
      and possible next steps or things to follow up. A few short paragraphs.
      Clinician-facing tone; clinical language is fine here.

  "codes_rationale": IF a list of reference ICD codes is provided below, a brief note
      on which of THOSE codes (and only those) best fit the material, and why, in a
      sentence or two each. Frame it explicitly as decision support for billing/coding
      consideration — NOT a diagnosis and NOT a billing determination. If no codes are
      provided, return an empty string. Never introduce a code that is not in the list.

  "client_recap": a detailed, warm, plain-language recap written directly to the client
      ("Today we talked about ..."). Cover, in a few short paragraphs: what you discussed
      and why it seems to matter to them; the main feelings and concerns they shared,
      reflected back with empathy; any strengths, insights, or helpful reframes that came
      up; the concrete next steps, ideas, or things to try that were agreed or suggested;
      and a brief encouraging close. Use supportive, validating, everyday language — NO
      diagnoses, NO ICD/DSM codes, no clinical jargon. Be specific to THIS conversation,
      not generic.

Rules:
- Ground everything strictly in the transcript. Do not invent events, quotes, or codes.
- The transcript labels each turn "Therapist:", "Client:", or "AI:".
- Output ONLY the raw JSON object — no code fences, no commentary before or after.
"""


def generate(transcript: str, surfaced_codes: list, mode: str = "solo") -> "dict | None":
    """Return {clinical, codes_rationale, client_recap} for the session, or None.

    `surfaced_codes` is a list of dicts with at least "label", "code", "source" —
    the ICD entries that actually appeared in-session. Never raises.
    """
    if not transcript or not transcript.strip():
        return None

    if surfaced_codes:
        code_lines = "\n".join(
            f"- {c.get('label', '?')}: {c.get('code', '')} ({c.get('source', '')})"
            for c in surfaced_codes
        )
        codes_block = "ICD reference codes that surfaced during this session:\n" + code_lines
    else:
        codes_block = "No ICD reference codes surfaced during this session."

    user_content = (
        f"Session type: {mode}.\n\n"
        f"Transcript (speaker-labelled):\n{transcript}\n\n"
        f"{codes_block}\n\n"
        "Produce the therapist-only summary JSON now."
    )

    try:
        client = _get_claude_client()
        response = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=3000,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text
    except Exception as exc:
        logger.warning("Clinical summary generation failed (%s); narrative omitted.", exc)
        return None

    return _parse(raw)


def _parse(raw: str) -> "dict | None":
    """Tolerantly parse the model's JSON object. None on any malformed output."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "clinical": str(data.get("clinical", "")).strip(),
        "codes_rationale": str(data.get("codes_rationale", "")).strip(),
        "client_recap": str(data.get("client_recap", "")).strip(),
    }
