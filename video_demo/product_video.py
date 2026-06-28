"""Full product/features demo video (narrated MP4 with synced captions).

Same pipeline as consent_video.py (Edge TTS narration timed to slides, ffmpeg
stitch, burned-in word-synced captions). Reuses the slide renderers from
build_consent_gif.py. The session screenshot (with illustrated avatars in the
video tiles) is zoom-spotlighted to show four features from one screen.

Run:     ../TogetherMindsAI.venv/Scripts/python product_video.py
Output:  ./output/product_demo.mp4  (+ .srt)
"""
import os
import asyncio
import shutil
import subprocess
import tempfile

import edge_tts
from PIL import Image

from build_consent_gif import (
    OUTDIR, ASSETS, ACCENT_2,
    slide_text, slide_links, slide_image, slide_screen,
)

VOICE = "en-US-AndrewNeural"
OUT = os.path.join(OUTDIR, "product_demo.mp4")
SRT = os.path.join(OUTDIR, "product_demo.srt")
PAD = 0.5
CAP_BAND = 120
VID_W, VID_H = 1280, 860 + CAP_BAND
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

# Session screen with illustrated avatars; zoom-spotlight regions (px in the 2872x1610 shot).
_SESSION = Image.open(os.path.join(ASSETS, "prod_session_avatars.png")).convert("RGB")
_CHAT = _SESSION.crop((15, 480, 985, 1450))       # the transcribed conversation
_COPILOT = _SESSION.crop((2326, 86, 2872, 1010))  # the private co-pilot panel
_ICD = _SESSION.crop((2310, 945, 2865, 1205))     # the ICD-10 / ICD-11 reference card

SLIDES = [
    (slide_text("TogetherMindsAI — the full picture",
                "An AI co-pilot for clinician-led sessions"),
     "Welcome to TogetherMindsAI: an AI co-pilot that supports licensed clinicians through every "
     "session — with secure audio and video, live notes, and a plan for every practice, with suggested "
     "analyses and I C D codes to speed up billing."),

    (slide_links("Read the full terms anytime",
                 [("Privacy Policy", "https://tm.onlinegbc.com/privacy"),
                  ("Terms of Service", "https://tm.onlinegbc.com/tos")],
                 sub="Everything is governed by these - linked on every page."),
     "First, the full Privacy Policy and Terms of Service are linked on every page, at t m dot online "
     "g b c dot com, slash privacy, and slash t o s — and anyone can read them at any time."),

    (slide_screen(f"{ASSETS}/prod_welcome.png", "1 - Clinician-led",
                  "Led by a licensed clinician",
                  "The AI assists in the background — it reflects and suggests; it never controls care "
                  "or replaces the clinician."),
     "Every session is led by a licensed clinician. The AI works quietly in the background to assist "
     "them. It reflects and suggests; it never controls care, and it never replaces the clinician."),

    (slide_image(_SESSION, "2 - Audio & video",
                 "Secure audio & video sessions",
                 "Encrypted calls in the browser — microphone, camera, and transcription — for "
                 "one-on-one, couple, or group sessions."),
     "Sessions run over secure, encrypted audio and video, right in the browser. The clinician controls "
     "the room — microphone, camera, and transcription — for one-on-one, couples, or group sessions."),

    (slide_image(_CHAT, "3 - Live transcription",
                 "Words become text, in real time",
                 "An accurate written record of the conversation, as it happens."),
     "As people speak, their words become text in real time, so the clinician always has an accurate "
     "written record — without taking their eyes off the conversation."),

    (slide_image(_COPILOT, "4 - AI Co-pilot",
                 "A private AI co-pilot",
                 "Visible only to the clinician — gentle suggestions, technique reminders, and "
                 "high-priority risk alerts. Clients never see it."),
     "Alongside the conversation, a private co-pilot — visible only to the clinician — surfaces gentle "
     "suggestions, technique reminders, and high-priority risk alerts. It reads tone to help set the "
     "right pace. Clients never see it."),

    (slide_image(_ICD, "5 - Summaries & coding",
                 "Summaries & billing codes, grounded in ICD-11",
                 "A clinical summary plus suggested ICD-10 / ICD-11 codes — documentation in minutes."),
     "After the session, the co-pilot drafts a clinical summary and suggests billing codes, grounded in "
     "the World Health Organization's I C D eleven — so documentation and coding take minutes, not hours."),

    (slide_screen(f"{ASSETS}/prod_pricing.png", "6 - Pricing",
                  "Plans for every practice",
                  "Free transcripts. Pro ($10) adds the co-pilot, summaries & billing codes. "
                  "Premium ($25) adds audio/video recording + 30-day storage."),
     "There's a plan for every practice. Free includes guided sessions and full transcripts. Pro, at ten "
     "dollars a month, adds the AI co-pilot, summaries, and billing codes. Premium, at twenty-five, adds "
     "secure audio and video recording with thirty-day storage."),

    (slide_text("Private & secure by design",
                "Encrypted - HIPAA-covered providers - never sold or used to train AI - clients consent first",
                accent=ACCENT_2),
     "And it's private by design. Everything is encrypted, handled by Hippa-covered providers under "
     "signed agreements, and never sold or used to train AI. Clients consent before anything begins. "
     "TogetherMindsAI — clinician-led, AI-assisted, built on trust."),
]


async def _synth(text, mp3):
    words = []
    with open(mp3, "wb") as f:
        async for ch in edge_tts.Communicate(text, VOICE, boundary="WordBoundary").stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                start = ch["offset"] / 1e7
                words.append((ch["text"], start, start + ch["duration"] / 1e7))
    return words


def tts(text, mp3):
    return asyncio.run(_synth(text, mp3))


def build_cues(words, offset, max_words=7, max_chars=42):
    cues, cur = [], []
    for w in words:
        cur.append(w)
        text = " ".join(x[0] for x in cur)
        if len(cur) >= max_words or len(text) >= max_chars:
            cues.append((cur[0][1] + offset, cur[-1][2] + offset, text))
            cur = []
    if cur:
        cues.append((cur[0][1] + offset, cur[-1][2] + offset, " ".join(x[0] for x in cur)))
    return cues


def ass_ts(s):
    cs = int(round(s * 100)); h, cs = divmod(cs, 360000); m, cs = divmod(cs, 6000); sec, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{sec:02d}.{cs:02d}"


def srt_ts(s):
    ms = int(round(s * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def write_ass(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(ASS_HEADER)
        for st, en, text in cues:
            en = max(en, st + 0.4)
            f.write(f"Dialogue: 0,{ass_ts(st)},{ass_ts(en)},Cap,,0,0,0,,{text}\n")


def write_srt(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, (st, en, text) in enumerate(cues, 1):
            f.write(f"{i}\n{srt_ts(st)} --> {srt_ts(max(en, st + 0.4))}\n{text}\n\n")


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", path],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def main():
    tmp = tempfile.mkdtemp(prefix="product_vid_")
    print(f"Voice: {VOICE}\nTemp:  {tmp}\n")
    segments, cues, t0 = [], [], 0.0
    for i, (img, narration) in enumerate(SLIDES):
        png = os.path.join(tmp, f"s{i:02d}.png"); mp3 = os.path.join(tmp, f"s{i:02d}.mp3"); seg = os.path.join(tmp, f"v{i:02d}.mp4")
        img.save(png)
        print(f"  [{i+1}/{len(SLIDES)}] {narration[:58]}...")
        words = tts(narration, mp3)
        dur = duration(mp3) + PAD
        cues += build_cues(words, t0); t0 += dur
        subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", png, "-i", mp3, "-t", f"{dur:.3f}",
                        "-r", "24", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
                        "-b:a", "192k", "-map", "0:v:0", "-map", "1:a:0", seg],
                       check=True, capture_output=True)
        segments.append(seg); print(f"          {dur:.1f}s")

    listfile = os.path.join(tmp, "list.txt")
    with open(listfile, "w") as f:
        for s in segments:
            f.write(f"file '{s}'\n")
    raw = os.path.join(tmp, "raw.mp4")
    print("\nStitching slides...")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", raw],
                   check=True, capture_output=True)

    write_ass(cues, os.path.join(tmp, "captions.ass")); write_srt(cues, SRT)
    print("Burning captions...")
    vf = f"pad={VID_W}:{VID_H}:0:0:color=0x0D1117,subtitles=captions.ass"
    subprocess.run(["ffmpeg", "-y", "-i", "raw.mp4", "-vf", vf, "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "copy", OUT], cwd=tmp, check=True, capture_output=True)

    total = duration(OUT); size = os.path.getsize(OUT) / 1e6
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nDone!  ->  {OUT}\nCaptions: {SRT}\nDuration: {total:.0f}s   Size: {size:.1f} MB")


if __name__ == "__main__":
    main()
