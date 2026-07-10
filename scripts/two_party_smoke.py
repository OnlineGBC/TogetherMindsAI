"""
scripts/two_party_smoke.py
==========================
Drive a full two-party clinician-led session end to end in two real browsers,
so a solo developer can test without running between machines (and without audio
echo — this path uses no mic/camera).

  - THERAPIST → Brave, with a PERSISTENT profile, so you only sign in once and
    stay signed in across runs.
  - CLIENT    → Playwright's bundled Chromium in a fresh (incognito-like) context.

It first prompts you to OPEN the two browsers, then to LOG IN to both (Google /
Microsoft OAuth can't be scripted). After that it automates the rest:

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
        # 1) Prompt to OPEN the two browsers ----------------------------------
        pause("Press Enter and I'll OPEN the two browser windows:\n"
              "      • Brave    → the THERAPIST\n"
              "      • Chromium → the CLIENT")

        # THERAPIST: Brave, persistent profile (login persists across runs).
        ther_ctx = p.chromium.launch_persistent_context(
            user_data_dir=BRAVE_PROFILE,
            executable_path=BRAVE_PATH,
            headless=False,
            no_viewport=True,
            ignore_https_errors=True,
            args=["--new-window"],
        )
        ther = ther_ctx.new_page()
        ther.goto(BASE + "/login")

        # CLIENT: bundled Chromium, fresh context (incognito-like).
        cli_browser = p.chromium.launch(headless=False)
        cli_ctx = cli_browser.new_context(ignore_https_errors=True, no_viewport=True)
        cli = cli_ctx.new_page()
        cli.goto(BASE + "/client/login")

        # 2) Prompt to LOG IN to both ------------------------------------------
        pause("Both windows are open on their sign-in pages. Now LOG IN:\n"
              "      • BRAVE    → sign in as the THERAPIST (Google / Microsoft)\n"
              "      • CHROMIUM → sign in as the CLIENT\n"
              "    Then come back here.")

        # Make sure the THERAPIST actually signed in before starting a session.
        ther.goto(BASE + "/therapist")
        while "/login" in ther.url:
            pause("The THERAPIST (Brave) isn't signed in yet — finish it in Brave.")
            ther.goto(BASE + "/therapist")

        # 3) Therapist starts a session ---------------------------------------
        ther.check("#agreeTherapist")
        ther.click(f'button.mode-btn[data-mode="{MODE}"]')
        ther.wait_for_url(f"**/therapy/{MODE}/**", timeout=30000)
        sid = ther.url.rstrip("/").split("/")[-1]
        print(f"\n[✓] Therapist started a {MODE} session: {sid}")

        # 4) Client joins → consent + state attestation -----------------------
        cli.goto(f"{BASE}/therapy/{MODE}/{sid}")           # → redirects to consent
        for _ in range(4):
            try:
                cli.wait_for_selector("#stateSelect", timeout=12000)
                break
            except Exception:
                # not on the consent page — usually the CLIENT isn't signed in yet
                pause("The CLIENT (Chromium) isn't signed in yet — finish it in Chromium.")
                cli.goto(f"{BASE}/therapy/{MODE}/{sid}")
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
