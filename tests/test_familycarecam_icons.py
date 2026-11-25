"""
tests/test_familycarecam_icons.py
---------------------------------
The site and Android icons are the FamilyCareCam icon (red heart + family on teal),
not the old TogetherMindsAI heart-and-pulse icon. Checks sizes, the teal background,
and that the Android notification icons are a plain white shape (Android tints them).
"""
import os
import pytest

Image = pytest.importorskip("PIL.Image")

ROOT = os.path.dirname(os.path.dirname(__file__))
RES = os.path.join(ROOT, "app", "src", "main", "res")
TEAL = (0, 150, 136)

WEB = {
    "static/icons/icon-192.png": 192, "static/icons/icon-512.png": 512,
    "static/icons/apple-touch-icon-152.png": 152,
    "static/icons/apple-touch-icon-167.png": 167,
    "static/icons/apple-touch-icon-180.png": 180,
    "store_icon.png": 512,
}
LAUNCHER = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
MASKABLE = {"mdpi": 82, "hdpi": 123, "xhdpi": 164, "xxhdpi": 246, "xxxhdpi": 328}
SPLASH = {"mdpi": 300, "hdpi": 450, "xhdpi": 600, "xxhdpi": 900, "xxxhdpi": 1200}
NOTIF = {"mdpi": 24, "hdpi": 36, "xhdpi": 48, "xxhdpi": 72, "xxxhdpi": 96}


def _open(path):
    return Image.open(os.path.join(ROOT, path)).convert("RGBA")


@pytest.mark.parametrize("path,size", WEB.items())
def test_web_and_store_icons_are_teal_squares_of_the_right_size(path, size):
    im = _open(path)
    assert im.size == (size, size)
    # Full-bleed teal: the corner is solid teal. The old icon had transparent corners.
    assert im.getpixel((0, 0))[:3] == TEAL and im.getpixel((0, 0))[3] == 255
    # The heart is red somewhere in the middle (not the old art, not blank).
    reds = [im.getpixel((size // 2, y)) for y in range(size // 3, size)]
    assert any(p[0] > 200 and p[1] < 90 for p in reds)


@pytest.mark.parametrize("density,size", LAUNCHER.items())
def test_launcher_icons(density, size):
    im = _open(f"app/src/main/res/mipmap-{density}/ic_launcher.png")
    assert im.size == (size, size)
    assert im.getpixel((0, 0))[3] == 0  # rounded corners stay transparent


@pytest.mark.parametrize("density,size", MASKABLE.items())
def test_maskable_icons_fill_the_square_with_teal(density, size):
    im = _open(f"app/src/main/res/mipmap-{density}/ic_maskable.png")
    assert im.size == (size, size)
    assert im.getpixel((0, 0))[:3] == TEAL


@pytest.mark.parametrize("density,size", SPLASH.items())
def test_splash_images(density, size):
    im = _open(f"app/src/main/res/drawable-{density}/splash.png")
    assert im.size == (size, size)
    assert im.getpixel((size // 2, int(size * 0.08)))[:3] == TEAL


@pytest.mark.parametrize("density,size", NOTIF.items())
def test_notification_icons_are_one_white_shape(density, size):
    im = _open(f"app/src/main/res/drawable-{density}/ic_notification_icon.png")
    assert im.size == (size, size)
    shown = [p for p in im.getdata() if p[3] > 0]
    assert shown, "icon is empty"
    # Android uses only the shape: every visible pixel must be white (no red or teal).
    assert all(p[0] == p[1] == p[2] == 255 for p in shown)


def test_adaptive_icon_background_is_teal_not_white():
    xml = open(os.path.join(RES, "mipmap-anydpi-v26", "ic_launcher.xml"), encoding="utf-8").read()
    assert 'android:drawable="#009688"' in xml
    assert "@android:color/white" not in xml


def test_favicon_is_the_new_icon_not_the_old_pulse_heart():
    svg = open(os.path.join(ROOT, "static", "favicon.svg"), encoding="utf-8").read()
    assert "#009688" in svg and "#E53E3E" in svg
    assert "polyline" not in svg  # the old icon's heartbeat line
