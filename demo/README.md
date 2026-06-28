# Demo media — patient consent experience

Tooling that generates a short demo of how a **patient gives consent** in
TogetherMindsAI (session recording, AI documentation, HIPAA), in plain language.

Two outputs, both built from the real app screens in `assets/` (captured from
`Disclaimers.docx`) with the actual disclosure wording:

| Script | Output | What it is |
|---|---|---|
| `build_consent_gif.py` | `output/patient_consent_demo.gif` | Silent annotated GIF walkthrough |
| `consent_video.py` | `output/patient_consent_demo.mp4` | Narrated MP4 (voiceover + slides) |

The narrated MP4 reuses the approach from the `YogurtVideo` project: Microsoft
**Edge TTS** (free, no API key) for the voiceover, then **ffmpeg** times each
slide to its narration and stitches the video. `consent_video.py` imports its
slide renderers from `build_consent_gif.py`, so the GIF and MP4 stay visually
identical.

## Setup

```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # edge-tts + pillow
```

`ffmpeg` and `ffprobe` must be on PATH (system install — not a pip package).

## Build

```
.venv/Scripts/python build_consent_gif.py    # -> output/patient_consent_demo.gif
.venv/Scripts/python consent_video.py         # -> output/patient_consent_demo.mp4
```

## Notes

- `assets/` holds the four real app screens (welcome, join disclaimer, the
  "Before you join" consent gate, the record-consent prompt). The consent-gate
  image is the older modal; the live app now shows the same text full-page.
- Change the narration voice via `VOICE` in `consent_video.py` (any Edge TTS
  voice, e.g. `en-US-AriaNeural`, `en-IN-NeerjaNeural`, `en-US-GuyNeural`).
- `output/` and `.venv/` are gitignored (generated / environment).
