"""
scripts/two_party_smoke.py
==========================
Drive a full two-party clinician-led session end to end in two real browsers,
so a solo developer can test without running between machines (and without audio
echo — this path uses no mic/camera).

  - THERAPIST → Brave, with a PERSISTENT profile, so you only sign in once and
    stay signed in across runs.
  - CLIENT    → Playwright's bundled Chromium in a fresh (incognito-like) context.

The script pauses for you to complete each OAuth sign-in by hand (Google /
Microsoft can't be scripted), then automates the rest:

  therapist starts a group session → client joins → consent + U.S. state
  attestation → held at the licensure gate → therapist's certify prompt appears
  (via the heartbeat) → certify → client is admitted to the room.

It leaves both windows open at the end so you can poke around manually.

--------------------------------------------------------------------------------
Prerequisites (one time)
--------------------------------------------------------------------------------
  # bundled Chromium for the CLIENT side (Brave is used as-is, no install)
  ./TogetherMindsAI.venv/Scripts/python.exe -m playwright install chromium

--------------------------------------------------------------------------------
Run
--------------------------------------------------------------------------------
  ./TogetherMindsAI.venv/Scripts/python.exe scripts/two_party_smoke.py

Environment overrides (all optional):
  TM_BASE_URL   base URL to test        (default: the live Cloud Run URL)
  TM_MODE       solo|couple|group       (default: group)
  TM_STATE      client's USPS state     (default: NJ)
  BRAVE_PATH    Brave executable path   (default: standard Windows install)

  # test against a local dev server instead of production:
  TM_BASE_URL=https://localhost:5001 ./...python.exe scripts/two_party_smoke.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = (len(sys.argv) > 1 and sys.argv[1]) or os.environ.get(
    "TM_BASE_URL", "https://togethermindsai-pofun7pgcq-uc.a.run.app"
)
BASE = BASE.rstrip("/")
MODE = os.environ.get("TM_MODE", "group")
STATE = os.environ.get("TM_STATE", "NJ").upper()
BRAVE_PATH = os.environ.get(
    "BRAVE_PATH",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
)
# Persistent Brave profile → sign in once, stay signed in on later runs.
BRAVE_PROFILE = os.path.join(os.path.expanduser("~"), ".tm_smoke_brave")


def pause(msg: str) -> None:
    print("\n>>> " + msg)
    input("    (press Enter to continue) ")


def main() -> None:
    print(f"Target : {BASE}")
    print(f"Mode   : {MODE}   Client state: {STATE}")
    if not os.path.exists(BRAVE_PATH):
        print(f"\nBrave not found at:\n  {BRAVE_PATH}\n"
              f"Set BRAVE_PATH to your Brave executable and re-run.")
        sys.exit(1)

    with sync_playwright() as p:
        # ---- THERAPIST: Brave, persistent profile (login persists) -----------
        ther_ctx = p.chromium.launch_persistent_context(
            user_data_dir=BRAVE_PROFILE,
            executable_path=BRAVE_PATH,
            headless=False,
            no_viewport=True,
            ignore_https_errors=True,
            args=["--new-window"],
        )
        ther = ther_ctx.new_page()

        # ---- CLIENT: bundled Chromium, fresh context (incognito-like) --------
        cli_browser = p.chromium.launch(headless=False)
        cli_ctx = cli_browser.new_context(ignore_https_errors=True, no_viewport=True)
        cli = cli_ctx.new_page()

        # 1) Therapist sign-in (manual OAuth) ---------------------------------
        ther.goto(BASE + "/login")
        pause("BRAVE window: sign in as the THERAPIST (Google / Microsoft).")
        ther.goto(BASE + "/therapist")
        if "/login" in ther.url:
            pause("Not signed in yet — finish the THERAPIST login in Brave.")
            ther.goto(BASE + "/therapist")

        # 2) Start a session ---------------------------------------------------
        ther.check("#agreeTherapist")
        ther.click(f'button.mode-btn[data-mode="{MODE}"]')
        ther.wait_for_url(f"**/therapy/{MODE}/**", timeout=30000)
        sid = ther.url.rstrip("/").split("/")[-1]
        print(f"\n[✓] Therapist started a {MODE} session: {sid}")

        # 3) Client sign-in (manual OAuth) ------------------------------------
        cli.goto(BASE + "/client/login")
        pause("CHROMIUM window: sign in as the CLIENT (or use a second Google account).")

        # 4) Client joins → consent + state attestation -----------------------
        cli.goto(f"{BASE}/therapy/{MODE}/{sid}")           # → redirects to consent
        cli.wait_for_selector("#stateSelect", timeout=30000)
        cli.select_option("#stateSelect", STATE)
        cli.check("#locationAttest")
        cli.click('button:has-text("I understand and agree")')
        cli.wait_for_url("**/state-gate", timeout=30000)
        print(f"[✓] Client consented from {STATE} — held at the licensure gate.")

        # 5) Therapist certification prompt (surfaced by the heartbeat) -------
        print("[…] Waiting for the therapist's certification prompt (heartbeat, ≤15s)…")
        ther.wait_for_selector("#stateCertYes", state="visible", timeout=30000)
        who = (ther.text_content("#stateCertName") or "").strip()
        print(f"[✓] Certify prompt shown for: {who}")
        ther.click("#stateCertYes")

        # 6) Client admitted to the room --------------------------------------
        cli.wait_for_url(f"**/therapy/{MODE}/{sid}", timeout=30000)
        print(f"[✓] Client admitted to the room.\n\n    ALL GREEN — two-party session established.")

        pause("Poke around in both windows (chat, co-pilot, Progress, etc.). "
              "Enter closes the browsers.")
        cli_browser.close()
        ther_ctx.close()


if __name__ == "__main__":
    main()
