"""
recording.py
------------
Phase 4 — session A/V recording via the self-hosted LiveKit Egress service.

Starts/stops a room-composite recording (MP4) that Egress uploads directly to the
private recordings bucket. Calls are made synchronously over LiveKit's Twirp HTTP
API (via `requests`), which is safe under the app's eventlet workers — unlike the
async livekit-api client. Never raises: returns None/False on failure so a
recording control can never crash a live session.
"""

import logging
import requests

from livekit.api import AccessToken, VideoGrants

import config

logger = logging.getLogger(__name__)


def _http_base() -> str:
    """LiveKit HTTP (Twirp) endpoint, derived from the ws(s) URL."""
    url = (config.LIVEKIT_URL or "").strip()
    return url.replace("wss://", "https://").replace("ws://", "http://").rstrip("/")


def _egress_jwt() -> str:
    """A short-lived token authorizing egress (recording) operations."""
    return (
        AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
        .with_identity("recorder")
        .with_grants(VideoGrants(room_record=True))
        .to_jwt()
    )


def _twirp(method: str, body: dict) -> dict:
    resp = requests.post(
        f"{_http_base()}/twirp/livekit.Egress/{method}",
        headers={"Authorization": "Bearer " + _egress_jwt(), "Content-Type": "application/json"},
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def start_recording(room_name: str, filepath: str):
    """Start a room-composite recording → MP4 uploaded to the recordings bucket.

    `filepath` is the object path within the bucket. Egress uploads using the VM's
    own identity (credentials left empty → Application Default Credentials).
    Returns the egress id, or None on failure.
    """
    try:
        data = _twirp("StartRoomCompositeEgress", {
            "room_name": room_name,
            "file_outputs": [{
                "file_type": "MP4",
                "filepath": filepath,
                "gcp": {"bucket": config.RECORDINGS_BUCKET, "credentials": ""},
            }],
        })
        return data.get("egressId") or data.get("egress_id")
    except Exception as exc:
        logger.warning("start_recording failed: %s", exc)
        return None


def stop_recording(egress_id: str) -> bool:
    """Stop an active recording. Returns True on success."""
    if not egress_id:
        return False
    try:
        _twirp("StopEgress", {"egress_id": egress_id})
        return True
    except Exception as exc:
        logger.warning("stop_recording failed: %s", exc)
        return False
