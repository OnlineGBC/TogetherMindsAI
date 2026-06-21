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


# ---------------------------------------------------------------------------
# Recordings-bucket access (Phase 4 Step 3) — download + retention deletion.
# Uses google-cloud-storage with Application Default Credentials (the Cloud Run
# service account). The client is built lazily so importing this module never
# requires GCS credentials (tests mock these functions). Never raises.
# ---------------------------------------------------------------------------

def _bucket():
    from google.cloud import storage
    return storage.Client().bucket(config.RECORDINGS_BUCKET)


def download_stream(object_path: str):
    """Stream a recordings-bucket object in chunks for an in-app download.

    Returns (generator, size_bytes, content_type), or (None, 0, None) on failure
    (missing object, no credentials, etc.). The bytes are read in chunks so a
    large MP4 is never fully buffered in memory.
    """
    try:
        blob = _bucket().blob(object_path)
        blob.reload()   # populates size/content_type; raises if the object is gone
        size = blob.size or 0
        ctype = blob.content_type or "video/mp4"

        def _gen():
            with blob.open("rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk

        return _gen(), size, ctype
    except Exception as exc:
        logger.warning("download_stream failed: %s", exc)
        return None, 0, None


def download_bytes(object_path: str):
    """Load a recordings-bucket object fully into memory for a reliable in-app
    download. Returns (BytesIO, size, content_type), or (None, 0, None) on failure.

    Unlike download_stream (a generator), this avoids streaming the object through
    the app's eventlet worker — that streaming was crashing mid-response (HTTP 500).
    Fine for session-length recordings; revisit for multi-hour files.
    """
    import io
    try:
        blob = _bucket().blob(object_path)
        data = blob.download_as_bytes()             # raises if the object is gone
        ctype = blob.content_type or "video/mp4"
        return io.BytesIO(data), len(data), ctype
    except Exception as exc:
        logger.warning("download_bytes failed (%s): %s", object_path, exc)
        return None, 0, None


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
