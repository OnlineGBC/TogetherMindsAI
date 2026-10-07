"""
tests/test_audio_route.py
-------------------------
The other person's voice must play through the right speaker.

Reported from a live test (Oct 2026): a patient on a Bluetooth speaker heard
her own voice played back and never heard the therapist; the therapist heard
her fine. Zoom on the same laptop and speaker worked.

Cause: the speaker has its own mic. Windows used it, which flips the speaker
into "phone call" (hands-free) mode — it plays that mic back to itself and its
normal output goes silent. We played the therapist to the normal default output.

The routing choice lives in static/js/audio-route.js (pure JS, run here under
node); the wiring lives in session_live.html (read as source, like
test_stt_reconnect.py).
"""
import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)
ROUTE_JS = os.path.abspath(os.path.join(HERE, "..", "static", "js", "audio-route.js"))
ROOM = os.path.join(HERE, "..", "templates", "session_live.html")

NODE = shutil.which("node")


def pick(mic, devices):
    """Run AudioRoute.pickOutput under node and return its answer."""
    if not NODE:
        pytest.skip("node not installed")
    script = (
        "const r = require(process.argv[1]);"
        "const a = JSON.parse(process.argv[2]);"
        "process.stdout.write(JSON.stringify(r.pickOutput(a.mic, a.devices)));"
    )
    out = subprocess.run(
        [NODE, "-e", script, ROUTE_JS, json.dumps({"mic": mic, "devices": devices})],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def out(device_id, label, group="g"):
    return {"kind": "audiooutput", "deviceId": device_id, "label": label, "groupId": group}


def inp(device_id, label, group="g"):
    return {"kind": "audioinput", "deviceId": device_id, "label": label, "groupId": group}


# A Windows laptop with broken built-in speakers and a Bluetooth speaker that
# has its own mic — the setup from the report.
BT_SPEAKER = [
    out("default", "Default - Speakers (JBL Flip 5 Stereo)", "jbl"),
    out("communications", "Communications - Headset (JBL Flip 5 Hands-Free)", "jbl"),
    out("a2dp", "Speakers (JBL Flip 5 Stereo)", "jbl"),
    out("hfp", "Headset (JBL Flip 5 Hands-Free)", "jbl"),
    out("realtek", "Speakers (Realtek(R) Audio)", "rt"),
    inp("default", "Default - Headset (JBL Flip 5 Hands-Free)", "jbl"),
    inp("btmic", "Headset (JBL Flip 5 Hands-Free)", "jbl"),
    inp("lapmic", "Microphone Array (Realtek(R) Audio)", "rt"),
]


def test_bluetooth_speaker_mic_plays_through_its_call_output():
    """The reported bug: the speaker's own mic is in use, so the voice must go to
    its hands-free output, not the silent stereo one."""
    assert pick({"label": "Headset (JBL Flip 5 Hands-Free)", "groupId": "jbl"}, BT_SPEAKER) == "hfp"


def test_laptop_mic_with_bluetooth_speaker_keeps_the_default():
    """Laptop mic: the speaker never enters call mode, so the default (the
    Bluetooth speaker) is right. Must NOT move to the laptop's own speakers,
    which in the report did not work."""
    assert pick({"label": "Microphone Array (Realtek(R) Audio)", "groupId": "rt"}, BT_SPEAKER) == ""


def test_laptop_mic_and_laptop_speakers_keep_the_default():
    devices = [
        out("default", "Default - Speakers (Realtek(R) Audio)", "rt"),
        out("realtek", "Speakers (Realtek(R) Audio)", "rt"),
        inp("lapmic", "Microphone Array (Realtek(R) Audio)", "rt"),
    ]
    assert pick({"label": "Microphone Array (Realtek(R) Audio)", "groupId": "rt"}, devices) == ""


def test_bluetooth_headset_goes_to_its_own_call_output():
    devices = [
        out("default", "Default - Headphones (WH-1000XM4 Stereo)", "sony"),
        out("st", "Headphones (WH-1000XM4 Stereo)", "sony"),
        out("hf", "Headset (WH-1000XM4 Hands-Free AG Audio)", "sony"),
        inp("hfmic", "Headset (WH-1000XM4 Hands-Free AG Audio)", "sony"),
    ]
    assert pick({"label": "Headset (WH-1000XM4 Hands-Free AG Audio)", "groupId": "sony"}, devices) == "hf"


def test_falls_back_to_group_when_names_differ():
    devices = [
        out("st", "Speakers (Flip Stereo)", "jbl"),
        out("hf", "Headphones (Flip Hands-Free AG)", "jbl"),
    ]
    assert pick({"label": "Headset (Flip Hands-Free)", "groupId": "jbl"}, devices) == "hf"


def test_falls_back_to_windows_communications_output():
    devices = [out("communications", "Communications - Headset (Flip Hands-Free)", "x")]
    assert pick({"label": "Headset (Speaker Hands-Free)", "groupId": "other"}, devices) == "communications"


def test_mac_or_usb_mic_keeps_the_default():
    devices = [out("default", "Default - MacBook Pro Speakers", "m"), out("usb", "USB Audio", "u")]
    assert pick({"label": "MacBook Pro Microphone", "groupId": "m"}, devices) == ""
    assert pick({}, []) == ""


# ---------------------------------------------------------------------------
# Wiring in the session room
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def src():
    return open(ROOM, encoding="utf-8").read()


def test_room_loads_the_routing_script(src):
    assert "js/audio-route.js" in src
    assert src.index("js/audio-route.js") < src.index("AudioRoute.pickOutput")


def test_incoming_voice_is_sent_to_the_chosen_speaker(src):
    start = src.index("RoomEvent.TrackSubscribed")
    handler = src[start:src.index("RoomEvent.TrackUnsubscribed")]
    assert "_sinkTo(" in handler
    assert "setSinkId(audioSinkId)" in src


def test_routing_runs_on_join_on_mic_recovery_and_on_device_change(src):
    join = src[src.index("async function joinAudio("):src.index("async function _startSTT(")]
    assert "await routeAudio()" in join
    recover = src[src.index("async function recoverMic("):]
    recover = recover[:recover.index("updateMicPill();\n    }")]
    assert "await routeAudio()" in recover
    assert 'addEventListener("devicechange"' in src
