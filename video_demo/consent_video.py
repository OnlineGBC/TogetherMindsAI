"""Build a narrated MP4 walkthrough of the patient consent experience.

Reuses the slide renderers from build_consent_gif.py and the narration/assembly
approach from the YogurtVideo project: Microsoft Edge TTS (free, no API key) for
voiceover, then ffmpeg to time each slide to its narration and stitch the video.

Requirements:  pip install edge-tts pillow   (ffmpeg + ffprobe must be on PATH)
Run:           python consent_video.py
Output:        ./output/patient_consent_demo.mp4
"""
import os
import asyncio
import shutil
import subprocess
import tempfile

import edge_tts

from build_consent_gif import (
    HERE, OUTDIR, ASSETS, ACCENT_2,
    slide_text, slide_screen, slide_links,
)

# Warm, confident US-English male voice. Swap to any Edge TTS voice, e.g.
#   en-US-BrianNeural (warm male), en-US-AriaNeural / en-US-JennyNeural (female).
VOICE = "en-US-AndrewNeural"
OUT = os.path.join(OUTDIR, "patient_consent_demo.mp4")
PAD = 0.5            # trailing pause (seconds) added after each narration

# (slide image, narration) — narration is the spoken version of the on-screen caption.
# Note: captions keep the correct spelling "HIPAA"; the narration spells it "Hippa"
# so the text-to-speech voice says it as a word (HIP-uh), not letter by letter.
SLIDES = [
    (slide_text("How patients give consent",
                "TogetherMindsAI - recording, AI documentation & HIPAA, in plain language"),
     "Here's how patients give consent on TogetherMindsAI, in plain language: to session recording, "
     "to AI helping with documentation, and to how their health information is handled under Hippa."),

    (slide_links("Read the full terms anytime",
                 [("Privacy Policy", "tm.onlinegbc.com/privacy"),
                  ("Terms of Service", "tm.onlinegbc.com/tos")],
                 sub="Everything below is governed by these - linked on every page."),
     "First, the full Privacy Policy and Terms of Service are linked on every page, at t m dot online "
     "g b c dot com, slash privacy, and slash t o s. Everything here is governed by them, and a patient "
     "can read them at any time."),

    (slide_screen(f"{ASSETS}/welcome.png", "1 - Context",
                  "Every session is clinician-led",
                  "The therapist leads the session; the AI only assists them privately. "
                  "This framing is set before anyone joins.",
                  "Encrypted - never sold or used to train AI"),
     "Every session is clinician led. A licensed therapist runs it from start to finish, and the AI "
     "only assists the clinician in the background. Everything is encrypted, and a patient's information "
     "is never sold or used to train AI."),

    (slide_screen(f"{ASSETS}/auth_disclaimer.png", "2 - First gate",
                  "Confirm who's responsible - and what's kept",
                  "Before joining, the client confirms they are 18+ and that the session is led by a "
                  "licensed professional, whose confidential clinical record this becomes - encrypted, "
                  "retained up to 6 years.",
                  "-> Patient agrees to the Terms of Service and these points"),
     "When a patient joins, they confirm they are eighteen or older, that a licensed professional leads "
     "and is responsible for the session, and that their conversation becomes the clinician's confidential "
     "clinical record, encrypted and kept for up to six years."),

    (slide_screen(f"{ASSETS}/consent_gate.png", "3 - AI documentation",
                  "Explicit consent to live AI transcription",
                  "Plain-language disclosure: speech is transcribed to text by an automated AI service; "
                  "the session is NOT recorded by default; data is handled by HIPAA-covered providers under "
                  "signed agreements; transcription can be turned off anytime.",
                  "-> Patient taps 'I understand and agree'"),
     "Next, explicit consent to live AI transcription. Their speech is turned into text by an automated "
     "AI service, so the clinician has accurate notes. The session isn't recorded by default. Data is "
     "handled by Hippa covered providers under signed agreements, and the patient can turn transcription "
     "off anytime. Only on, I understand and agree, does the session begin."),

    (slide_screen(f"{ASSETS}/record_consent.png", "4 - Recording",
                  "Recording only with all-party consent",
                  "If the clinician asks to record, it starts only if everyone consents and stops the moment "
                  "anyone declines or withdraws. The recording joins the confidential record, is never sold or "
                  "used to train AI, and consent can be withdrawn at any time.",
                  "-> Patient chooses 'I consent' or 'Decline'"),
     "Recording audio or video is separate and optional. It begins only when everyone present consents, "
     "and stops the instant anyone declines or withdraws. A recording joins the confidential record, is "
     "never sold or used to train AI, and consent can be withdrawn at any moment."),

    (slide_text("Your data, your control",
                "Encrypted in transit and at rest - audio & video deleted in 30 days, transcript up to 6 years - "
                "never sold or used to train AI - turn transcription off or withdraw recording consent anytime."),
     "In short: encrypted in transit and at rest. Audio and video are deleted in thirty days; the transcript "
     "stays up to six years. Nothing is sold or used to train AI, and the patient can stop transcription "
     "or withdraw recording consent anytime."),

    (slide_text("Consent is explicit, plain-language & revocable",
                "Reviewed and agreed before every session - withdraw anytime.",
                accent=ACCENT_2),
     "That's the heart of it: consent that is explicit, plain-language, reviewed before every session, "
     "and always revocable."),
]


# Captions are burned into a band added below the slide, so they never overlap the
# slide's own caption panel. The band colour matches the app-dark background.
CAP_BAND = 120                       # extra pixels added at the bottom for captions
VID_W, VID_H = 1280, 860 + CAP_BAND  # final video size (slide + caption band)
SRT = os.path.join(OUTDIR, "patient_consent_demo.srt")

# ASS pins the play resolution to the real video size, so Fontsize is in actual
# pixels (an SRT would be scaled up from libass's tiny default resolution).
ASS_HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {VID_W}
PlayResY: {VID_H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial,30,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,60,60,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


async def _synth(text, mp3):
    """Synthesize narration AND collect per-word timing (Edge TTS WordBoundary)."""
    words = []
    with open(mp3, "wb") as f:
        async for ch in edge_tts.Communicate(text, VOICE, boundary="WordBoundary").stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                start = ch["offset"] / 1e7          # 100-ns ticks -> seconds
                words.append((ch["text"], start, start + ch["duration"] / 1e7))
    return words


def tts(text, mp3):
    return asyncio.run(_synth(text, mp3))


def build_cues(words, offset, max_words=7, max_chars=42):
    """Group word timings into short, readable caption cues, shifted by `offset`
    (the slide's start time on the final timeline)."""
    cues, cur = [], []
    for w in words:
        cur.append(w)
        text = " ".join(x[0] for x in cur)
        if len(cur) >= max_words or len(text) >= max_chars:
            cues.append((cur[0][1] + offset, cur[-1][2] + offset, text))
            cur = []
    if cur:
        text = " ".join(x[0] for x in cur)
        cues.append((cur[0][1] + offset, cur[-1][2] + offset, text))
    return cues


def srt_ts(s):
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def write_srt(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        for idx, (st, en, text) in enumerate(cues, 1):
            en = max(en, st + 0.4)
            f.write(f"{idx}\n{srt_ts(st)} --> {srt_ts(en)}\n{text}\n\n")


def ass_ts(s):
    cs = int(round(s * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{sec:02d}.{cs:02d}"


def write_ass(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(ASS_HEADER)
        for st, en, text in cues:
            en = max(en, st + 0.4)
            f.write(f"Dialogue: 0,{ass_ts(st)},{ass_ts(en)},Cap,,0,0,0,,{text}\n")


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def main():
    tmp = tempfile.mkdtemp(prefix="consent_vid_")
    print(f"Voice: {VOICE}\nTemp:  {tmp}\n")
    segments, cues, global_t = [], [], 0.0
    for i, (img, narration) in enumerate(SLIDES):
        png = os.path.join(tmp, f"slide_{i:02d}.png")
        mp3 = os.path.join(tmp, f"slide_{i:02d}.mp3")
        seg = os.path.join(tmp, f"seg_{i:02d}.mp4")
        img.save(png)
        print(f"  [{i+1}/{len(SLIDES)}] narrating: {narration[:58]}...")
        words = tts(narration, mp3)
        dur = duration(mp3) + PAD
        cues += build_cues(words, global_t)
        global_t += dur
        # One slide held for the length of its narration (+ a short pause).
        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", png, "-i", mp3,
             "-t", f"{dur:.3f}", "-r", "24", "-pix_fmt", "yuv420p",
             "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
             "-map", "0:v:0", "-map", "1:a:0", seg],
            check=True, capture_output=True)
        segments.append(seg)
        print(f"          {dur:.1f}s")

    listfile = os.path.join(tmp, "segments.txt")
    with open(listfile, "w") as f:
        for s in segments:
            f.write(f"file '{s}'\n")
    raw = os.path.join(tmp, "raw.mp4")
    print("\nStitching slides...")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", raw],
        check=True, capture_output=True)

    # Captions: write an ASS (for precise burning) + an SRT sidecar for the user.
    write_ass(cues, os.path.join(tmp, "captions.ass"))
    write_srt(cues, SRT)
    print("Burning captions...")
    vf = (f"pad={VID_W}:{VID_H}:0:0:color=0x0D1117,subtitles=captions.ass")
    subprocess.run(
        ["ffmpeg", "-y", "-i", "raw.mp4", "-vf", vf,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", OUT],
        cwd=tmp, check=True, capture_output=True)

    total = duration(OUT)
    size = os.path.getsize(OUT) / 1e6
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nDone!  ->  {OUT}")
    print(f"Captions: {SRT}")
    print(f"Duration: {total:.0f}s   Size: {size:.1f} MB")


if __name__ == "__main__":
    main()
