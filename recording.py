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
            # Explicit encoding instead of LiveKit's 1080p default. Codecs are left
            # unset so LiveKit picks the right ones for MP4 (H.264 + AAC); pinning
            # them here risks a container/codec mismatch for no benefit.
            "advanced": {
                "width": config.RECORDING_WIDTH,
                "height": config.RECORDING_HEIGHT,
                "framerate": config.RECORDING_FRAMERATE,
                "video_bitrate": config.RECORDING_VIDEO_KBPS,
                "audio_bitrate": config.RECORDING_AUDIO_KBPS,
            },
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


# ---------------------------------------------------------------------------
# Recordings-bucket access (Phase 4 Step 3) — download + retention deletion.
# Uses google-cloud-storage with Application Default Credentials (the Cloud Run
# service account). The client is built lazily so importing this module never
# requires GCS credentials (tests mock these functions). Never raises.
# ---------------------------------------------------------------------------

def _bucket():
    from google.cloud import storage
    return storage.Client().bucket(config.RECORDINGS_BUCKET)


def signed_download_url(object_path: str, minutes: int = 15, filename: str = None):
    """Return a short-lived V4 signed URL for downloading the object DIRECTLY from
    GCS, or None on failure. The browser downloads straight from storage, so the
    file never passes through the app — this avoids Cloud Run's ~32 MiB response
    cap that was failing large recordings ('Response size was too large').

    Signing uses IAM SignBlob (the Cloud Run service account signs the URL); the
    service account needs the 'Service Account Token Creator' role on itself.
    Never raises.
    """
    try:
        from datetime import timedelta
        import google.auth
        from google.auth.transport import requests as ga_requests

        creds, _ = google.auth.default()
        creds.refresh(ga_requests.Request())   # ensure we have a fresh access token
        disposition = f'attachment; filename="{filename}"' if filename else None
        return _bucket().blob(object_path).generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=minutes),
            method="GET",
            service_account_email=getattr(creds, "service_account_email", None),
            access_token=getattr(creds, "token", None),
            response_disposition=disposition,
        )
    except Exception as exc:
        logger.warning("signed_download_url failed (%s): %s", object_path, exc)
        return None


def delete_object(object_path: str) -> bool:
    """Delete a recordings-bucket object (retention expiry). Returns True on
    success, or True if it was already gone; False only on a real error."""
    if not object_path:
        return False
    try:
        from google.cloud.exceptions import NotFound
        try:
            _bucket().blob(object_path).delete()
        except NotFound:
            return True   # already deleted — the desired end state
        return True
    except Exception as exc:
        logger.warning("delete_object failed: %s", exc)
        return False
