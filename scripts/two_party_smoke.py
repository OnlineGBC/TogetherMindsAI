"""
scripts/two_party_smoke.py
==========================
Drive a full two-party clinician-led session end to end across TWO browsers you
open yourself, so a solo developer can test without running between machines (and
without audio echo — this path uses no mic/camera).

You are in control of the browsers:

  1. The script prints two launch commands and prompts you to OPEN each browser
     yourself and SIGN IN:
        • THERAPIST → Brave
        • CLIENT    → Chromium (isolated profile — an incognito-like second identity)
  2. You report back (press Enter) after each.
  3. The script ATTACHES to those browsers over the debug port and automates the
     rest: start a session → client joins → consent + U.S. state attestation →
     licensure gate → therapist certifies → client admitted.

It never opens or closes your browsers — it only drives them. They stay open at
the end for manual poking.

--------------------------------------------------------------------------------
Why the launch commands? — the script can only attach to a browser that was
started with a remote-debugging port. A normal double-click won't expose one.
The dedicated --user-data-dir keeps these test profiles separate from your real
browsing AND keeps you signed in across runs.
--------------------------------------------------------------------------------

Run:
  ./TogetherMindsAI.venv/Scripts/python.exe scripts/two_party_smoke.py

Environment overrides (all optional):
  TM_BASE_URL   base URL to test        (default: the live Cloud Run URL)
  TM_MODE       solo|couple|group       (default: group)
  TM_STATE      client's USPS state     (default: NJ)
  BRAVE_PATH    Brave executable path   (default: standard Windows install)
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = ((len(sys.argv) > 1 and sys.argv[1]) or
        os.environ.get("TM_BASE_URL", "https://togethermindsai-pofun7pgcq-uc.a.run.app")).rstrip("/")
MODE = os.environ.get("TM_MODE", "group")
STATE = os.environ.get("TM_STATE", "NJ").upper()
BRAVE_PATH = os.environ.get(
    "BRAVE_PATH",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
)

THER_PORT, CLI_PORT = 9222, 9223
HOME = os.path.expanduser("~")
THER_DIR = os.path.join(HOME, ".tm_smoke_brave")       # therapist profile (persists login)
CLI_DIR = os.path.join(HOME, ".tm_smoke_chromium")     # client profile (isolated identity)


def pause(msg: str) -> None:
    print("\n>>> " + msg)
    input("    (press Enter when done) ")


def _launch_cmd(exe: str, port: int, profile: str) -> str:
    # remote-allow-origins=* is required by Chromium 111+ for the debug websocket.
    return (f'& "{exe}" --remote-debugging-port={port} '
            f'--remote-allow-origins=* --user-data-dir="{profile}"')


def _page(browser):
    """A fresh tab in the browser's existing (signed-in) context."""
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return ctx.new_page()


def main() -> None:
    print(f"Target : {BASE}")
    print(f"Mode   : {MODE}   Client state: {STATE}")
    if not os.path.exists(BRAVE_PATH):
        print(f"\nBrave not found at:\n  {BRAVE_PATH}\n"
              f"Set BRAVE_PATH to your Brave executable and re-run.")
        sys.exit(1)

    with sync_playwright() as p:
        chromium_exe = p.chromium.executable_path      # bundled Chromium for the client

        print("\n" + "=" * 74)
        print("STEP 1 — open the THERAPIST browser (Brave). Paste this into PowerShell:\n")
        print("   " + _launch_cmd(BRAVE_PATH, THER_PORT, THER_DIR))
        print("\nThen SIGN IN as the therapist (Google / Microsoft).")
        print("=" * 74)
        pause("Opened Brave and signed in as the THERAPIST?")

        print("\n" + "=" * 74)
        print("STEP 2 — open the CLIENT browser (Chromium). Paste this into PowerShell:\n")
        print("   " + _launch_cmd(chromium_exe, CLI_PORT, CLI_DIR))
        print("\nThen SIGN IN as the client (a second Google account is fine).")
        print("=" * 74)
        pause("Opened Chromium and signed in as the CLIENT?")

        # ---- Attach to both browsers over the debug port --------------------
        def connect(port, label):
            for _ in range(4):
                try:
                    return p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                except Exception as e:
                    pause(f"Couldn't attach to the {label} browser on port {port} "
                          f"({type(e).__name__}). Make sure you launched it with the command "
                          f"above (it must include --remote-debugging-port={port}), then retry.")
            print(f"Gave up attaching to the {label} browser on port {port}.")
            sys.exit(1)

        ther = _page(connect(THER_PORT, "THERAPIST"))
        cli = _page(connect(CLI_PORT, "CLIENT"))

        # ---- Make sure the therapist actually signed in ---------------------
        ther.goto(BASE + "/therapist")
        while "/login" in ther.url:
            pause("The THERAPIST (Brave) isn't signed in yet — finish it in Brave.")
            ther.goto(BASE + "/therapist")

        # ---- Therapist starts a session -------------------------------------
        ther.check("#agreeTherapist")
        ther.click(f'button.mode-btn[data-mode="{MODE}"]')
        ther.wait_for_url(f"**/therapy/{MODE}/**", timeout=30000)
        sid = ther.url.rstrip("/").split("/")[-1]
        print(f"\n[✓] Therapist started a {MODE} session: {sid}")

        # ---- Client joins → consent + state attestation ---------------------
        cli.goto(f"{BASE}/therapy/{MODE}/{sid}")           # → redirects to consent
        for _ in range(4):
            try:
                cli.wait_for_selector("#stateSelect", timeout=12000)
                break
            except Exception:
                pause("The CLIENT (Chromium) isn't signed in yet — finish it in Chromium.")
                cli.goto(f"{BASE}/therapy/{MODE}/{sid}")
        cli.select_option("#stateSelect", STATE)
        cli.check("#locationAttest")
        cli.click('button:has-text("I understand and agree")')
        cli.wait_for_url("**/state-gate", timeout=30000)
        print(f"[✓] Client consented from {STATE} — held at the licensure gate.")

        # ---- Therapist certification prompt (surfaced by the heartbeat) -----
        print("[…] Waiting for the therapist's certification prompt (heartbeat, ≤15s)…")
        ther.wait_for_selector("#stateCertYes", state="visible", timeout=30000)
        who = (ther.text_content("#stateCertName") or "").strip()
        print(f"[✓] Certify prompt shown for: {who}")
        ther.click("#stateCertYes")

        # ---- Client admitted to the room ------------------------------------
        cli.wait_for_url(f"**/therapy/{MODE}/{sid}", timeout=30000)
        print(f"[✓] Client admitted to the room.\n\n    ALL GREEN — two-party session established.")

        pause("Poke around in both windows (chat, co-pilot, Progress, …). "
              "Your browsers stay open; this just detaches.")
        # NB: we do NOT close the browsers — you opened them, you close them.


if __name__ == "__main__":
    main()
