# Demo media — patient consent experience

Tooling that generates a short demo of how a **patient gives consent** in
TogetherMindsAI (session recording, AI documentation, HIPAA), in plain language.

Two outputs, both built from the real app screens in `assets/` (captured from
`Disclaimers.docx`) with the actual disclosure wording:

| Script | Output | What it is |
|---|---|---|
| `build_consent_gif.py` | `output/patient_consent_demo.gif` | Silent annotated GIF walkthrough |
| `consent_video.py` | `output/patient_consent_demo.mp4` (+ `.srt`) | Narrated MP4 with burned-in captions |

The narrated MP4 reuses the approach from the `YogurtVideo` project: Microsoft
**Edge TTS** (free, no API key) for the voiceover, then **ffmpeg** times each
slide to its narration and stitches the video. `consent_video.py` imports its
slide renderers from `build_consent_gif.py`, so the GIF and MP4 stay visually
identical.

Captions are synced from Edge TTS **word timings** (`boundary="WordBoundary"`),
written to an ASS file, and burned into a band added below the slide so they
never overlap the slide's own caption panel. A reusable `.srt` is also written
next to the MP4.

## Setup

This shares the repo's single virtualenv, `TogetherMindsAI.venv`. All packages are
listed in the repo's main `requirements.txt` (this video uses `edge-tts` + `pillow`):

```
../TogetherMindsAI.venv/Scripts/python -m pip install -r ../requirements.txt
```

`ffmpeg` and `ffprobe` must be on PATH (system install — not a pip package).

## Build

Run from this folder with the repo venv (scripts resolve their own paths):

```
../TogetherMindsAI.venv/Scripts/python build_consent_gif.py   # -> output/patient_consent_demo.gif
../TogetherMindsAI.venv/Scripts/python consent_video.py        # -> output/patient_consent_demo.mp4
```

## Notes

- `assets/` holds the four real app screens (welcome, join disclaimer, the
  "Before you join" consent gate, the record-consent prompt). The consent-gate
  image is the older modal; the live app now shows the same text full-page.
- Change the narration voice via `VOICE` in `consent_video.py` (any Edge TTS
  voice, e.g. `en-US-AriaNeural`, `en-IN-NeerjaNeural`, `en-US-GuyNeural`).
- `output/` and the moved-in media in `assets/` are gitignored (root `.gitignore`);
  only the PNG source screenshots are tracked.
