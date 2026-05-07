"""
audit_viewport_fit.py
---------------------
One-off audit: visit every page at common viewports and report which ones
overflow vertically or horizontally.

Usage:
    python scripts/audit_viewport_fit.py

Requires the dev server running at https://127.0.0.1:5001 (the default).
"""
from playwright.sync_api import sync_playwright

BASE_URL = "https://127.0.0.1:5001"

VIEWPORTS = [
    ("1920x1080 desktop",   1920, 1080),
    ("1366x768 laptop",     1366, 768),
    ("1280x800 macbook",    1280, 800),
    ("414x896 iPhone 11",   414, 896),
    ("375x667 iPhone SE",   375, 667),
    ("412x915 Pixel 7",     412, 915),
    ("412x892 Pixel 7 Pro", 412, 892),
    ("360x780 Galaxy S23",  360, 780),
]

PUBLIC_PAGES = [
    ("Home",        "/"),
    ("Auth (solo)", "/auth/solo"),
    ("Auth (couple)","/auth/couple"),
    ("Auth (group)","/auth/group"),
    ("Join session","/session/join"),
    ("Feedback",    "/feedback"),
    ("Privacy",     "/privacy"),
    ("ToS",         "/tos"),
]


def measure_overflow(page):
    """Return (vertical_overflow_px, horizontal_overflow_px)."""
    return page.evaluate("""
        () => {
            const d = document.documentElement;
            return {
                vert: d.scrollHeight - d.clientHeight,
                horiz: d.scrollWidth - d.clientWidth,
                scrollH: d.scrollHeight,
                clientH: d.clientHeight,
                scrollW: d.scrollWidth,
                clientW: d.clientWidth,
            };
        }
    """)


def authenticate(page, mode):
    """Click through /auth/<mode> consent flow and return the resulting therapy URL."""
    page.goto(f"{BASE_URL}/auth/{mode}", wait_until="networkidle")
    # Tick the 3 consent boxes
    for cid in ["ageCheck", "aiCheck", "dataCheck"]:
        page.locator(f"#{cid}").check()
    # Click continue and wait for navigation to /therapy/<mode>/<sid>
    with page.expect_navigation(url=lambda u: "/therapy/" in u, timeout=15000):
        page.locator("#continueBtn").click()
    return page.url  # the therapy URL


def main():
    results = {}  # page_label -> [(viewport_label, vert, horiz)]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)

        # ---- Public pages ------------------------------------------------
        for label, path in PUBLIC_PAGES:
            results[label] = []
            for vp_label, w, h in VIEWPORTS:
                page = context.new_page()
                page.set_viewport_size({"width": w, "height": h})
                try:
                    page.goto(f"{BASE_URL}{path}", wait_until="networkidle", timeout=10000)
                    m = measure_overflow(page)
                    results[label].append((vp_label, m))
                except Exception as exc:
                    results[label].append((vp_label, f"ERROR: {exc}"))
                page.close()

        # ---- Auth-required pages: solo / couple / group + their progress ----
        for mode in ("solo", "couple", "group"):
            therapy_label = f"Reflection ({mode})"
            progress_label = f"Progress ({mode})"
            results[therapy_label] = []
            results[progress_label] = []

            # Use one persistent context per mode so the auth cookie sticks.
            mode_ctx = browser.new_context(ignore_https_errors=True)
            auth_page = mode_ctx.new_page()
            try:
                therapy_url = authenticate(auth_page, mode)
                # Extract the user_id by reading the localStorage / page (it's also the session id for solo)
                # Simpler: read from URL — /therapy/<mode>/<sid>
                sid = therapy_url.rstrip("/").split("/")[-1]
                # User-id ≠ session-id in couple/group, but for solo they're equal.
                # For an auth audit, the session_id/user_id mapping doesn't matter — we just need a valid auth cookie.
                progress_url = f"{BASE_URL}/progress/{sid}/{mode}"
            except Exception as exc:
                for vp_label, _, _ in VIEWPORTS:
                    results[therapy_label].append((vp_label, f"AUTH ERROR: {exc}"))
                    results[progress_label].append((vp_label, f"AUTH ERROR: {exc}"))
                auth_page.close()
                mode_ctx.close()
                continue
            auth_page.close()

            for label, url in [(therapy_label, therapy_url), (progress_label, progress_url)]:
                for vp_label, w, h in VIEWPORTS:
                    page = mode_ctx.new_page()
                    page.set_viewport_size({"width": w, "height": h})
                    try:
                        page.goto(url, wait_until="networkidle", timeout=10000)
                        m = measure_overflow(page)
                        results[label].append((vp_label, m))
                    except Exception as exc:
                        results[label].append((vp_label, f"ERROR: {exc}"))
                    page.close()
            mode_ctx.close()

        browser.close()

    # ---- Print report ----------------------------------------------------
    print(f"\n{'='*78}")
    print("VIEWPORT FIT AUDIT")
    print(f"{'='*78}\n")
    print(f"{'PAGE':<22} {'VIEWPORT':<22} {'V-OVERFLOW':>11} {'H-OVERFLOW':>11}  STATUS")
    print("-" * 78)
    for page_label, rows in results.items():
        for vp_label, m in rows:
            if isinstance(m, str):
                print(f"{page_label:<22} {vp_label:<22} {'?':>11} {'?':>11}  {m}")
            else:
                vert = m["vert"]
                horiz = m["horiz"]
                status = "OK" if vert <= 0 and horiz <= 0 else "OVERFLOW"
                if horiz > 0 and vert > 0:
                    status = "BOTH"
                elif vert > 0:
                    status = "VERTICAL"
                elif horiz > 0:
                    status = "HORIZONTAL"
                print(f"{page_label:<22} {vp_label:<22} {vert:>11} {horiz:>11}  {status}")
    print()


if __name__ == "__main__":
    main()
