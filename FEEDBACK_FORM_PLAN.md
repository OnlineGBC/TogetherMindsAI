# Feedback Form — Implementation Plan

## Scope
Email-only feedback form. No DB storage, no audit log entry. No IP / user_id / PII captured.

## Files to change
- `TogetherMindsAI.py` — `GET /feedback` route + `POST /api/feedback` endpoint with SMTP send
- `templates/feedback.html` — standalone form page
- `templates/_feedback_modal.html` — partial included by solo/couple/group templates
- `templates/base.html` — "Send feedback" link in footer
- `static/js/feedback.js` — platform detection, fetch submit, modal trigger after End Session
- `templates/solo.html`, `couple.html`, `group.html` — include modal partial; wire trigger
- `config.py` — read `FEEDBACK_SMTP_USER`, `FEEDBACK_SMTP_PASSWORD` from env. Hardcode `smtp.gmail.com:587` and `FEEDBACK_TO_EMAIL=raja@onlinegbc.com` (env override for local)
- `backend/tests/test_feedback.py` — new test file

## Form fields
1. Rating: 1–5 star buttons + "N/A — didn't use a session" radio
2. What worked well — textarea, ≤1000 chars, char counter
3. What could be improved — textarea, ≤1000 chars
4. What additional features would you want — textarea, ≤1000 chars
5. Would you pay for longer access or more features — Yes / Maybe / No (skip = leave unselected)
6. Anything else — textarea, ≤1000 chars

Privacy note at top: "We don't collect your IP, name, or any session content. This goes to our team email."

## Endpoint behavior (`POST /api/feedback`)
- Validates: rating ∈ {1,2,3,4,5,null}, would_pay ∈ {yes,maybe,no,null}, platform ∈ {web,android_twa,ios_pwa,mobile_browser}, mode ∈ {solo,couple,group,null}, each text ≤1000 chars, ≥1 field has content
- Reject otherwise → 400
- Builds plain-text email, sends via `smtplib.SMTP("smtp.gmail.com", 587)` STARTTLS, 5 s timeout
- `From: $FEEDBACK_SMTP_USER`, `To: raja@onlinegbc.com`
- SMTP failure → 503 generic error
- No audit entry, no DB write, no `request.remote_addr` access anywhere on this path

## Email format (text/plain)
```
Subject: TogetherMindsAI feedback — <mode>/<platform> — <rating>/5

Rating:           4 / 5
Would pay:        Maybe
Platform:         android_twa
Session mode:     solo
Submitted at:     2026-05-06 18:42 UTC

What worked:
<text>

What could be improved:
<text>

Desired features:
<text>

Other:
<text>
```
User-supplied text never appears in subject — only safe metadata. Plain text only (no HTML render).

## Rate limiting
In-memory cooldown keyed by Flask session cookie: max 1 submission / 60 s, max 10 / day per session. No IP. Per-process only (matches `voice_usage_seconds_today` pattern).

## End-of-session modal
- Hooks into existing `initEndSessionGuard` flow (solo/couple/group)
- Opens after End Session confirmation, before redirect to `/progress/...`
- Pre-fills `mode` from page JS context
- "Skip" → redirect immediately, zero side effect
- "Submit" → send feedback, then redirect

## Platform detection (client-side)
- `document.referrer.startsWith("android-app://")` → `android_twa`
- `window.navigator.standalone === true` → `ios_pwa`
- `/Mobi|Android|iPhone/.test(navigator.userAgent) && !standalone` → `mobile_browser`
- else → `web`

Server validates against the four allowed values.

## Tests (`backend/tests/test_feedback.py`)
1. Valid full submission → 200, `smtplib.SMTP` mock asserts to/from/subject/body
2. Rating null (N/A) accepted
3. Invalid rating (0, 6, "abc") → 400
4. Invalid would_pay / platform / mode → 400
5. Oversized text field → 400
6. All-empty submission → 400
7. Cooldown: second submission within 60 s → 429
8. SMTP raises → 503, no partial state
9. **Privacy contract**: submit with fake IP and UA in test client; email body contains none of IP, UA, session cookie value, user_id; no DB row in any table
10. `request.remote_addr` never logged on success path

## Manual steps after push
1. Generate Gmail App Password (Google account → Security → 2-Step Verification → App passwords)
2. Add GCP secrets `FEEDBACK_SMTP_USER` and `FEEDBACK_SMTP_PASSWORD`
3. Update `cloudbuild.yaml` / Cloud Run deploy to inject the two secrets (YAML edit prepared by Claude; `gcloud builds submit` run manually)

## Out of scope
- No DB model / migration / admin view
- No retention policy
- No audit log entry
- No transactional email service (Gmail SMTP only)
- English only
