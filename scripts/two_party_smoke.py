"""
scripts/two_party_smoke.py
==========================
Drive a two-party clinician-led session across TWO browsers you open yourself, so
a solo developer can test without running between machines (and without audio echo
— this path uses no mic/camera).

You are in control of the browsers and the therapist actions:

  1. You OPEN each browser yourself (the script prints the launch commands) and
     report back:
        • THERAPIST → Brave        (sign in)
        • CLIENT    → Chromium     (NO sign-in — joins by the shared name)
  2. In Brave you MANUALLY start a session and give it a FRIENDLY NAME (the ✏️
     control in the session header) — the name you'd share with a real client.
  3. You type that friendly name to the script.
  4. The script then attaches over the debug port and automates the rest:
        client joins by name (anonymously) → consent + U.S. state attestation →
        licensure gate → therapist certifies → client admitted.

It never opens or closes your browsers — it only drives them.

--------------------------------------------------------------------------------
Why the launch commands? — the script can only attach to a browser started with a
remote-debugging port; a normal double-click won't expose one. The dedicated
--user-data-dir keeps these test profiles separate from your real browsing and
keeps the THERAPIST signed in across runs.
--------------------------------------------------------------------------------

Run:
  ./TogetherMindsAI.venv/Scripts/python.exe scripts/two_party_smoke.py

Environment overrides (all optional):
  TM_BASE_URL   base URL to test        (default: the live Cloud Run URL)
  TM_STATE      client's USPS state     (default: NJ)
  BRAVE_PATH    Brave executable path   (default: standard Windows install)
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = ((len(sys.argv) > 1 and sys.argv[1]) or
        os.environ.get("TM_BASE_URL", "https://togethermindsai-pofun7pgcq-uc.a.run.app")).rstrip("/")
STATE = os.environ.get("TM_STATE", "NJ").upper()
BRAVE_PATH = os.environ.get(
    "BRAVE_PATH",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
)

THER_PORT, CLI_PORT = 9222, 9223
HOME = os.path.expanduser("~")
THER_DIR = os.path.join(HOME, ".tm_smoke_brave")       # therapist profile (persists login)
CLI_DIR = os.path.join(HOME, ".tm_smoke_chromium")     # client profile (fresh identity)


def pause(msg: str) -> None:
    print("\n>>> " + msg)
    input("    (press Enter when done) ")


def ask(msg: str) -> str:
    print("\n>>> " + msg)
    return input("    > ").strip()


def _launch_cmd(exe: str, port: int, profile: str) -> str:
    # remote-allow-origins=* is required by Chromium 111+ for the debug websocket.
    return (f'& "{exe}" --remote-debugging-port={port} '
            f'--remote-allow-origins=* --user-data-dir="{profile}"')


def _new_tab(browser):
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return ctx.new_page()


def _find_session_tab(browser):
    """The therapist's open session-room tab (URL contains /therapy/)."""
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                if "/therapy/" in pg.url:
                    return pg
            except Exception:
                pass
    return None


def main() -> None:
    print(f"Target : {BASE}")
    print(f"Client state: {STATE}")
    if not os.path.exists(BRAVE_PATH):
        print(f"\nBrave not found at:\n  {BRAVE_PATH}\n"
              f"Set BRAVE_PATH to your Brave executable and re-run.")
        sys.exit(1)

    with sync_playwright() as p:
        chromium_exe = p.chromium.executable_path      # bundled Chromium for the client

        print("\n" + "=" * 74)
        print("STEP 1 — open the THERAPIST browser (Brave). Paste into PowerShell:\n")
        print("   " + _launch_cmd(BRAVE_PATH, THER_PORT, THER_DIR))
        print("\nThen SIGN IN as the therapist (Google / Microsoft).")
        print("=" * 74)
        pause("Opened Brave and signed in as the THERAPIST?")

        print("\n" + "=" * 74)
        print("STEP 2 — open the CLIENT browser (Chromium). Paste into PowerShell:\n")
        print("   " + _launch_cmd(chromium_exe, CLI_PORT, CLI_DIR))
        print("\nNo sign-in needed — the client joins by the session name.")
        print("=" * 74)
        pause("Opened Chromium as the CLIENT?")

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

        ther_browser = connect(THER_PORT, "THERAPIST")
        cli_browser = connect(CLI_PORT, "CLIENT")

        # ---- STEP 3: therapist creates + names a session (manual) -----------
        print("\n" + "=" * 74)
        print("STEP 3 — in BRAVE, do this yourself:")
        print("   1. Start a session (Solo / Couple / Group).")
        print("   2. Set a FRIENDLY NAME with the  ✏️  control in the session header.")
        print("      (This is the name you'd share with a real client.)")
        print("=" * 74)
        friendly = ""
        while not friendly:
            friendly = ask("Type the FRIENDLY NAME you set (what the client will use to join):")

        ther = _find_session_tab(ther_browser)
        while ther is None:
            pause("I can't find an open session tab in Brave — make sure you started the session.")
            ther = _find_session_tab(ther_browser)
        print(f"[✓] Found the therapist's session tab: {ther.url}")

        # ---- STEP 4: client joins by name (anonymous, no OAuth) -------------
        cli = _new_tab(cli_browser)
        cli.goto(BASE + "/session/join")
        cli.fill("#session_id", friendly)
        cli.click("#rejoinForm button[type='submit']")

        # Anonymous identity page (skipped if this browser already has an identity).
        try:
            cli.wait_for_selector("#agreeAll", timeout=8000)
            cli.check("#agreeAll")
            cli.click("#continueBtn")
        except Exception:
            pass

        # Consent gate → state attestation.
        cli.wait_for_selector("#stateSelect", timeout=30000)
        cli.select_option("#stateSelect", STATE)
        cli.check("#locationAttest")
        cli.click('button:has-text("I understand and agree")')
        cli.wait_for_url("**/state-gate", timeout=30000)
        print(f"[✓] Client joined '{friendly}' from {STATE} — held at the licensure gate.")

        # ---- STEP 5: therapist certifies (auto, with manual fallback) -------
        print("[…] Waiting for the certification prompt in Brave (heartbeat, ≤15s)…")
        try:
            ther.bring_to_front()
            ther.wait_for_selector("#stateCertYes", state="visible", timeout=30000)
            who = (ther.text_content("#stateCertName") or "").strip()
            print(f"[✓] Certify prompt shown for: {who}")
            ther.click("#stateCertYes")
        except Exception:
            pause("In Brave, click 'I certify — admit the client' on the licensure prompt.")

        # ---- STEP 6: client admitted ----------------------------------------
        cli.wait_for_url("**/therapy/**", timeout=30000)
        print("[✓] Client admitted to the room.\n\n    ALL GREEN — two-party session established.")

        pause("Poke around in both windows (chat, co-pilot, Progress, …). "
              "Your browsers stay open; this just detaches.")
        # NB: we do NOT close the browsers — you opened them, you close them.


if __name__ == "__main__":
    main()
