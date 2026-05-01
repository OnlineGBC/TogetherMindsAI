"""
voice.py
--------
Browser audio -> English text via OpenAI Whisper (translate task).

Whisper auto-detects the source language (99 supported) and returns English
in a single API call. The English transcript is then routed through the
existing safety pipeline in ai_therapist.py (keyword crisis filter, secondary
Claude crisis check, referral guard, medical guard, off-topic guard) exactly
as for typed input.

Crisis keywords have been expanded with common translated-idiom phrases
(see ai_therapist.CRISIS_KEYWORDS) to mitigate the well-known weakness that
literal translation can strip clinical signal from non-English idioms.
"""

import logging
import os

logger = logging.getLogger(__name__)

_openai_client = None


def _get_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai_client


# Map browser-supplied MIME types to filenames Whisper recognises.
# Whisper accepts: mp3, mp4, mpeg, mpga, m4a, wav, webm.
_MIME_TO_FILENAME = {
    "audio/webm": "audio.webm",
    "audio/ogg": "audio.ogg",
    "audio/mp4": "audio.mp4",
    "audio/mpeg": "audio.mp3",
    "audio/mp3": "audio.mp3",
    "audio/wav": "audio.wav",
    "audio/x-wav": "audio.wav",
    "audio/m4a": "audio.m4a",
    "audio/x-m4a": "audio.m4a",
}


def transcribe_translate(audio_bytes: bytes, mime_type: str = "audio/webm") -> dict:
    """Transcribe and translate audio to English. Returns {'text', 'duration'}.

    Uses OpenAI Whisper's translate task — auto-detects the source language
    and returns English output. `duration` (seconds) is used by the route to
    track per-user daily usage against the cap.
    """
    client = _get_client()
    base_mime = (mime_type or "audio/webm").split(";")[0].strip().lower()
    filename = _MIME_TO_FILENAME.get(base_mime, "audio.webm")

    response = client.audio.translations.create(
        model="whisper-1",
        file=(filename, audio_bytes, base_mime),
        response_format="verbose_json",
    )

    text = getattr(response, "text", "") or ""
    duration = getattr(response, "duration", 0.0) or 0.0
    return {"text": text.strip(), "duration": float(duration)}
