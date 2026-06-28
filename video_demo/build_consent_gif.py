"""Build an annotated demo GIF of the patient consent experience from the real
app screens (in ./assets, originally from Disclaimers.docx).

Run:  python build_consent_gif.py
Output:  ./output/patient_consent_demo.gif
"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
OUTDIR = os.path.join(HERE, "output")
os.makedirs(OUTDIR, exist_ok=True)
OUT = os.path.join(OUTDIR, "patient_consent_demo.gif")

W, H = 1280, 860
BG       = (13, 17, 23)        # app dark
PANEL    = (22, 32, 30)
ACCENT   = (0, 200, 160)
ACCENT_2 = (124, 92, 237)
LINK     = (96, 165, 250)      # hyperlink blue
TEXT     = (232, 238, 236)
MUTED    = (157, 176, 170)

F_TITLE = ImageFont.truetype(r"C:/Windows/Fonts/arialbd.ttf", 46)
F_SUB   = ImageFont.truetype(r"C:/Windows/Fonts/arial.ttf",   26)
F_CAPB  = ImageFont.truetype(r"C:/Windows/Fonts/arialbd.ttf", 27)
F_CAP   = ImageFont.truetype(r"C:/Windows/Fonts/arial.ttf",   24)
F_TAG   = ImageFont.truetype(r"C:/Windows/Fonts/arialbd.ttf", 22)
F_LINK  = ImageFont.truetype(r"C:/Windows/Fonts/arialbd.ttf", 44)
F_LBL   = ImageFont.truetype(r"C:/Windows/Fonts/arial.ttf",   25)


def wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def rounded(draw, box, r, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def caption(img, tag, head, body, action=None):
    d = ImageDraw.Draw(img, "RGBA")
    bx = (40, 548, W - 40, 846)
    rounded(d, bx, 16, fill=(10, 14, 18, 238), outline=(ACCENT[0], ACCENT[1], ACCENT[2], 120), width=2)
    x, y = 64, 568
    if tag:
        tw = d.textlength(tag, font=F_TAG)
        rounded(d, (x, y, x + tw + 28, y + 32), 16, fill=(ACCENT_2[0], ACCENT_2[1], ACCENT_2[2], 230))
        d.text((x + 14, y + 4), tag, font=F_TAG, fill=(255, 255, 255))
        y += 44
    d.text((x, y), head, font=F_CAPB, fill=TEXT)
    y += 38
    for ln in wrap(d, body, F_CAP, W - 2 * x):
        d.text((x, y), ln, font=F_CAP, fill=MUTED)
        y += 29
    if action:
        d.text((x, y + 8), action, font=F_CAPB, fill=ACCENT)


def slide_screen(path, tag, head, body, action=None):
    return slide_image(Image.open(path).convert("RGB"), tag, head, body, action)


def slide_image(shot, tag, head, body, action=None):
    img = Image.new("RGB", (W, H), BG)
    shot = shot.convert("RGB")
    area_w, area_h = W - 60, 504
    sc = min(area_w / shot.width, area_h / shot.height)
    nw, nh = int(shot.width * sc), int(shot.height * sc)
    shot = shot.resize((nw, nh), Image.LANCZOS)
    ox, oy = (W - nw) // 2, 20 + (area_h - nh) // 2
    d = ImageDraw.Draw(img)
    rounded(d, (ox - 4, oy - 4, ox + nw + 4, oy + nh + 4), 10, outline=(60, 70, 68), width=2)
    img.paste(shot, (ox, oy))
    caption(img, tag, head, body, action)
    return img


def slide_links(head, links, sub=None):
    """A prominent slide whose URLs look like clickable hyperlinks (blue + underline).
    `links` is a list of (label, url)."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    rounded(d, (70, 150, W - 70, H - 150), 24, fill=PANEL, outline=(LINK[0], LINK[1], LINK[2]), width=3)
    ty = 210
    for ln in wrap(d, head, F_TITLE, W - 240):
        tw = d.textlength(ln, font=F_TITLE)
        d.text(((W - tw) // 2, ty), ln, font=F_TITLE, fill=TEXT)
        ty += 58
    if sub:
        for ln in wrap(d, sub, F_SUB, W - 320):
            tw = d.textlength(ln, font=F_SUB)
            d.text(((W - tw) // 2, ty + 6), ln, font=F_SUB, fill=MUTED)
            ty += 36
    ty += 26
    for label, url in links:
        lw = d.textlength(label, font=F_LBL)
        d.text(((W - lw) // 2, ty), label, font=F_LBL, fill=MUTED)
        ty += 38
        uw = d.textlength(url, font=F_LINK)
        ux = (W - uw) // 2
        d.text((ux, ty), url, font=F_LINK, fill=LINK)
        d.line((ux, ty + 52, ux + uw, ty + 52), fill=LINK, width=3)   # underline
        ty += 86
    return img


def slide_text(head, sub, accent=ACCENT):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    rounded(d, (80, 250, W - 80, H - 250), 24, fill=PANEL, outline=(accent[0], accent[1], accent[2]), width=3)
    lines = wrap(d, head, F_TITLE, W - 260)
    ty = (H - len(lines) * 60 - 60) // 2
    for ln in lines:
        tw = d.textlength(ln, font=F_TITLE)
        d.text(((W - tw) // 2, ty), ln, font=F_TITLE, fill=TEXT)
        ty += 60
    for ln in wrap(d, sub, F_SUB, W - 320):
        tw = d.textlength(ln, font=F_SUB)
        d.text(((W - tw) // 2, ty + 14), ln, font=F_SUB, fill=MUTED)
        ty += 36
    return img


def build():
  frames = [
    (slide_text("How patients give consent",
                "TogetherMindsAI - recording, AI documentation & HIPAA, in plain language"), 3600),
    (slide_links("Read the full terms anytime",
                 [("Privacy Policy", "tm.onlinegbc.com/privacy"),
                  ("Terms of Service", "tm.onlinegbc.com/tos")],
                 sub="Everything below is governed by these - linked on every page."), 6000),
    (slide_screen(f"{ASSETS}/welcome.png", "1 - Context",
                  "Every session is clinician-led",
                  "The therapist leads the session; the AI only assists them privately. "
                  "This framing is set before anyone joins.",
                  "Encrypted - never sold or used to train AI"), 5200),
    (slide_screen(f"{ASSETS}/auth_disclaimer.png", "2 - First gate",
                  "Confirm who's responsible - and what's kept",
                  "Before joining, the client confirms they are 18+ and that the session is led by a "
                  "licensed professional, whose confidential clinical record this becomes - encrypted, "
                  "retained up to 6 years.",
                  "-> Patient agrees to the Terms of Service and these points"), 6000),
    (slide_screen(f"{ASSETS}/consent_gate.png", "3 - AI documentation",
                  "Explicit consent to live AI transcription",
                  "Plain-language disclosure: speech is transcribed to text by an automated AI service; "
                  "the session is NOT recorded by default; data is handled by HIPAA-covered providers under "
                  "signed agreements; transcription can be turned off anytime.",
                  "-> Patient taps 'I understand and agree'"), 6800),
    (slide_screen(f"{ASSETS}/record_consent.png", "4 - Recording",
                  "Recording only with all-party consent",
                  "If the clinician asks to record, it starts only if everyone consents and stops the moment "
                  "anyone declines or withdraws. The recording joins the confidential record, is never sold or "
                  "used to train AI, and consent can be withdrawn at any time.",
                  "-> Patient chooses 'I consent' or 'Decline'"), 6800),
    (slide_text("Your data, your control",
                "Encrypted in transit and at rest - audio & video deleted in 30 days, transcript up to 6 years - "
                "never sold or used to train AI - turn transcription off or withdraw recording consent anytime."), 6200),
    (slide_text("Consent is explicit, plain-language & revocable",
                "Reviewed and agreed before every session - withdraw anytime.",
                accent=ACCENT_2), 4600),
  ]

  imgs = [f.convert("P", palette=Image.ADAPTIVE, colors=256) for f, _ in frames]
  durs = [d for _, d in frames]
  imgs[0].save(OUT, save_all=True, append_images=imgs[1:], duration=durs, loop=0, optimize=True, disposal=2)
  print("WROTE", OUT, "size(MB)=%.2f" % (os.path.getsize(OUT) / 1e6), "frames=", len(imgs))


if __name__ == "__main__":
    build()
