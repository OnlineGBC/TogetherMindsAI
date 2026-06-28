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


def tts(text, out_path):
    asyncio.run(edge_tts.Communicate(text, VOICE).save(out_path))


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def main():
    tmp = tempfile.mkdtemp(prefix="consent_vid_")
    print(f"Voice: {VOICE}\nTemp:  {tmp}\n")
    segments = []
    for i, (img, narration) in enumerate(SLIDES):
        png = os.path.join(tmp, f"slide_{i:02d}.png")
        mp3 = os.path.join(tmp, f"slide_{i:02d}.mp3")
        seg = os.path.join(tmp, f"seg_{i:02d}.mp4")
        img.save(png)
        print(f"  [{i+1}/{len(SLIDES)}] narrating: {narration[:58]}...")
        tts(narration, mp3)
        dur = duration(mp3) + PAD
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
    print("\nStitching final video...")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
         "-c", "copy", OUT],
        check=True, capture_output=True)

    total = duration(OUT)
    size = os.path.getsize(OUT) / 1e6
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nDone!  ->  {OUT}")
    print(f"Duration: {total:.0f}s   Size: {size:.1f} MB")


if __name__ == "__main__":
    main()
