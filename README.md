# TogetherMindsAI

A clinician-led **reflections** platform for live audio/video sessions. A practitioner (psychotherapist, hypnotherapist, or caregiver) runs a one-to-one, couple, or group session; an AI co-pilot gives the practitioner quiet suggestions, technique reminders, and risk alerts, and can write a session summary. Practitioners can also sign in from inside a hospital EHR (Epic, Oracle Health) and file a short summary into the patient's chart.

> TogetherMindsAI is a reflections app, not a therapy or medical app. It does not provide diagnosis or treatment. The practitioner leads; the AI supports their judgment.

---

## Where to go for questions

| Topic | Read this |
|---|---|
| **Logins: Google, Microsoft, Epic, Oracle Health (Cerner)** — what broke, how it was fixed, code locations, how to debug, how to add another sign-in | **[ComplianceAndDocumentation/Login-Authentication-Integration-Details.docx](ComplianceAndDocumentation/Login-Authentication-Integration-Details.docx)** |
| Local secrets (SOPS + Google Cloud KMS) | [SECRETS.md](SECRETS.md) |
| Session ID format | [SESSION_ID_REFERENCE.md](SESSION_ID_REFERENCE.md) |
| HIPAA risk analysis | [HIPAA_Risk_Analysis.md](HIPAA_Risk_Analysis.md) |
| Policies, risk reports, SRA export, NDA | [ComplianceAndDocumentation/](ComplianceAndDocumentation/) |

If sign-in fails for someone, start with section 4.8 of the login document (a log-query playbook).

---

## What it does

- **Live audio and video** — LiveKit, self-hosted on a Compute Engine VM (not on Cloud Run). Streaming speech-to-text (AssemblyAI) turns the conversation into a transcript; transcribing is the default, recording is separate and consent-gated.
- **Three session modes** — solo, couple, group.
- **Roles and plans** — the account's role decides what is part of their product; the plan decides what is unlocked (`roles.py`, `billing.py`).

  | Role | Free | Paid adds |
  |---|---|---|
  | Psychologist / Psychotherapist | live A/V, chat, transcript, safety alerts | AI co-pilot and recap, ICD/billing codes, recording |
  | Hypnotherapist / hypnotic coach | same as above | AI, recording (no ICD codes) |
  | Nurse / parent / caregiver | live watching only | recording (hours are metered, `hours.py`) |

  Accounts that sign in through Epic or Oracle Health get the full product automatically.
- **AI co-pilot** — private suggestion cards for the practitioner, grounded by an ICD/DSM reference layer (`copilot.py`, `clinical_reference.py`, `clinical_data/`). Crisis and risk cards are rule-based, not model output.
- **Session summaries** — a full AI summary (practitioner only) and a short chart summary for EHR notes (`clinical_summary.py`).
- **Transcripts** — PDF and Word downloads, delivered through opaque token links (`documents.py`).
- **Recording** — optional, all-party consent, self-hosted LiveKit Egress, 30-day retention (`recording.py`).
- **EHR chart notes** — see below.
- **Billing** — Stripe Checkout and the Stripe billing portal; no card data touches the app (`billing.py`, `routes_billing.py`).
- **Admin console** — comp (free) access, behind a second factor (`routes_admin.py`, `admin_access.py`).
- **Installable web app and Android app** — a PWA, plus a Trusted Web Activity wrapper in `app/`.
- **Feedback form** — modal plus email delivery (`feedback_email.py`).

---

## Sign-in

| Sign-in | Entry | Stored as | Notes |
|---|---|---|---|
| Google | `/auth/google/login` (clinicians), `/client/auth/google/login` (clients) | `google` | account picker after logout; one-time automatic retry; remembered-account hint |
| Microsoft | `/auth/microsoft/login`, `/client/auth/microsoft/login` | `microsoft` | multi-tenant issuer check |
| Epic | EHR launch at `/ehr/launch` | `epic` | SMART on FHIR |
| Oracle Health (Cerner) | EHR launch at `/ehr/launch` | `oracle` | SMART on FHIR, several Cerner-only fixes |

Each provider is a **separate TMAI account**, even for the same person. Code lives in `routes_oauth.py`, `routes_ehr.py`, `ehr.py`, and the `_oauth_*` helpers in `TogetherMindsAI.py`. **Full details, line numbers, and the Google troubleshooting playbook:** [Login-Authentication-Integration-Details.docx](ComplianceAndDocumentation/Login-Authentication-Integration-Details.docx).

### EHR chart notes (Epic and Oracle Health)

1. The clinician launches TMAI from the EHR (`/ehr/launch` → `/ehr/callback`) and lands on a "Launch worked" page showing the patient and encounter.
2. A green **Click here to start your TMAI Session** button opens TMAI in a new tab to run the session.
3. Back on the launch page, the clinician picks one of **their own** sessions (name and Session ID shown; **Refresh my sessions** updates the list) and presses **Load recap**. A button blinks green **Generating . . .** while the short summary is written.
4. The note reads: pointer and link to the full analysis, the short summary, billing-code considerations (CPT / place of service when known), and the standing last line *"The transcript should be reviewed for accuracy before filing."*
5. The clinician reviews and presses **File this note in the chart**. Nothing is filed until that button.

The link points to `/session/summary/<token>`, a readable full-analysis page for the clinician who led the session. It uses an opaque token, so the Session ID never appears in a chart. The launch page cannot be reloaded (the EHR's sign-in code is single use).

Switches: `EHR_ENABLED` turns the launch on; `EHR_WRITE_ENABLED` separately makes the chart writable. Both routes 404 when off.

---

## Claude models

Model names live in one place, `config.py`:

| Setting | Default | Used for |
|---|---|---|
| `CLAUDE_MODEL_SMART` | `claude-sonnet-5-5` | live co-pilot, full session summary, translation |
| `CLAUDE_MODEL_FAST` | `claude-haiku-5-5` | the short EHR chart summary |

Change one with an environment setting and a redeploy. Do not write a model name into a call site. These models think by default, so short calls turn thinking off and every reply is read as "the first text block" (`ai_therapist.claude_create`, `ai_therapist.claude_text`). Test any new model's behaviour before changing the default.

---

## Tech stack

| Layer | Local | Production (Cloud Run) |
|---|---|---|
| Runtime | local venv (`TogetherMindsAI.venv/`) | Python 3.10 (Docker) |
| WSGI | werkzeug dev server (threading) | gunicorn + eventlet (1 worker) |
| Database | SQLite (`instance/togethermindsai.db`) | PostgreSQL (Cloud SQL) |
| Realtime | Flask-SocketIO (polling) | Flask-SocketIO over WebSocket |
| Audio/video | — | self-hosted LiveKit (Compute Engine VM `tmai-rtc`), LiveKit Egress for recording |
| Speech-to-text | — | AssemblyAI streaming |
| AI | Anthropic Claude API | Anthropic Claude API |
| Payments | Stripe | Stripe |
| Secrets | `sops.env` (SOPS + GCP KMS) | GCP Secret Manager |

Browser scripts (socket.io, Bootstrap, Chart.js, LiveKit) are self-hosted in `static/vendor/` so they work with privacy browsers.

---

## Repository layout

```
TogetherMindsAI.py        # Flask + SocketIO app: sessions, live room, co-pilot hooks, summaries, OAuth helpers
config.py                 # every environment setting, feature switch, and the Claude model names
models.py                 # SQLAlchemy models; Fernet field encryption
routes_oauth.py           # Google / Microsoft sign-in routes (clinicians and clients)
routes_ehr.py, ehr.py     # Epic / Oracle Health launch, sign-in, and chart-note filing
routes_billing.py, billing.py, billing_codes.py, hours.py   # Stripe, plans, CPT/POS lookups, caregiver hours
routes_admin.py, admin_access.py                            # comp-access console
roles.py                  # roles, capabilities, what each plan unlocks
ai_therapist.py           # shared Claude client, safe reply reader, crisis keyword guards
copilot.py                # co-pilot cards and answers
clinical_summary.py       # full and short (EHR) summaries
clinical_reference.py     # ICD/DSM grounding; data in clinical_data/
recording.py              # LiveKit Egress recording control
documents.py              # PDF / Word transcript rendering
audit.py, log_filter.py   # audit log; log-redaction filter
feedback_email.py         # feedback email formatting
session_id.py             # session ID generation and parsing
templates/, static/       # Jinja pages; JS, CSS, and vendored libraries
tests/                    # pytest suite (about 1,350 tests)
scripts/                  # one-off tools (system-prompt token check, ICD harvest, smoke scripts)
video_demo/               # patient-consent demo video builder
app/, build.gradle, ...   # Android TWA wrapper and release builds
ComplianceAndDocumentation/  # policies, risk reports, SRA export, NDA, the login document
Dockerfile, cloudbuild.yaml
```

---

## Local development

Assumes the venv is already activated.

1. Install dependencies (first time, and when `requirements.txt` changes):
   ```powershell
   pip install -r requirements.txt
   pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
   ```
2. **Local secrets are encrypted at rest** with SOPS + Google Cloud KMS — see [SECRETS.md](SECRETS.md). There is **no plaintext `.env`**. Edit a secret with `sops sops.env`.
3. Run the app (secrets are decrypted into memory only):
   ```powershell
   sops exec-env sops.env 'python TogetherMindsAI.py'
   ```
4. Open `http://127.0.0.1:5000`.

### Tests

```powershell
python -m pytest tests/ -v
```

The full suite is the pre-deploy gate: run it green before every deploy. New behaviour comes with a test in the same commit. Sign-in tests are in `tests/test_auth_oauth.py`; EHR tests in `tests/test_ehr_launch.py`, `tests/test_ehr_write.py`, `tests/test_ehr_summary_page.py`; model handling in `tests/test_claude_models.py`. Tests use fake provider and model responses, so confirm sign-in and the AI wording on a real device and a real session after a deploy.

---

## Deploy (Google Cloud Run)

Deploys are **manual** — pushing to `main` does **not** deploy.

```powershell
gcloud builds submit --config cloudbuild.yaml .
```

`cloudbuild.yaml` builds the image, pushes it to Artifact Registry, and deploys to Cloud Run with secrets injected from Secret Manager (see the file for the list and for instance, memory, and Cloud SQL settings).

- `CORS_ALLOWED_ORIGINS` must include the custom domain (`https://tm.onlinegbc.com`) or SocketIO fails.
- The service worker's cache name changes with every deploy, so stale scripts clear themselves. Stylesheets also carry a `?v=` token in `templates/base.html` and `templates/base_embed.html`: change it when you change CSS.
- After a deploy that touches sign-in, test on a real Android phone once after it has been idle.

---

## Android (TWA)

The Android app is a Trusted Web Activity: a thin shell that opens the deployed site in Chrome, with no native screens. **It has no storage of its own** — cookies, cache, and the Google sign-in belong to Chrome — so "Clear storage" on the app does nothing. See the login document, section 4.6, for how to really clear it. Build files: `twa-manifest.json`, `build.gradle`, `app/`; release artifacts (`app-release-signed.apk`, `app-release-bundle.aab`) are checked in at the repo root.

---

## Security and compliance notes

- Session cookie: `HttpOnly`, `SameSite=Lax`, `Secure` in production, 30-day lifetime; automatic logoff after 30 minutes idle (`IDLE_TIMEOUT_SECONDS`); the session is cleared on login (anti session-fixation).
- Field-level encryption (Fernet) on stored chat messages, summaries, co-pilot cards, display names, and session labels.
- Content-Security-Policy enforced with a per-request nonce; HSTS in production; `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`.
- Per-IP rate limits (flask-limiter).
- Clinician-led session records are kept about 6 years; recordings 30 days after a session's last recording; a scheduled job purges what has expired.
- EHR launch only accepts allow-listed FHIR servers (`EHR_ALLOWED_ISS`); EHR tokens are stored encrypted in the database and dropped after the note is filed; only FHIR ids are kept, never patient names or birth dates.
- Chart notes: only the clinician who led a session can load it into a note; the Session ID never appears in a chart.
- Local-dev secrets encrypted at rest with SOPS + GCP KMS.
- Regenerate the risk audit with `python generate_risk_audit.py`.

### Known gaps and parked work

- **Log redaction:** `log_filter.py` is installed on the root logger but is known not to reach the app logger, so PHI redaction in logs is not in effect yet.
- HIPAA paperwork still to write: risk analysis, written policies, disaster-recovery runbook, breach process, training records, customer-facing BAA template.
- Crisis/risk cards are silenced live by the "Chattiness" Less/Stop setting; safety alerts should bypass it.
- Partners cannot yet see their own earnings or payout schedule.
- A "newer Claude model available" log warning is parked (see the Claude models section for why models are not switched automatically).

---

## License

See [LICENSE](LICENSE).
