# HIPAA Security Risk Analysis — TogetherMindsAI

> **Status: WORKING DRAFT for internal review.** This document is a starting point
> prepared from the system's current technical implementation. It is **not legal
> advice** and must be reviewed, corrected, and approved by the organization's
> designated HIPAA Security Officer and legal/compliance counsel before it is
> relied upon. Fields in `[brackets]` require your input.

| | |
|---|---|
| **Organization (Covered Entity / Business Associate)** | Global Business Consulting, Inc. (GBCai4org) |
| **HIPAA role** | Business Associate to the licensed clinicians (Covered Entities) who use the app |
| **System** | TogetherMindsAI — clinician-led teletherapy / reflections platform |
| **Document owner (Security Officer)** | `[name, title, contact]` |
| **Version / Date** | 0.1 (draft) — 2026-07-26 |
| **Review cadence** | At least annually, and after any significant change to systems, threats, or a security incident |
| **Regulatory basis** | HIPAA Security Rule §164.308(a)(1)(ii)(A) (risk analysis) and (B) (risk management); methodology aligned with NIST SP 800-30 |

---

## 1. Purpose & scope

Identify reasonably anticipated threats and vulnerabilities to the confidentiality,
integrity, and availability of electronic Protected Health Information (ePHI) created,
received, maintained, or transmitted by TogetherMindsAI; assess current safeguards;
and define a plan to reduce residual risk to a reasonable and appropriate level.

**In scope:** the TogetherMindsAI web application, its Google Cloud infrastructure
(Cloud Run, Cloud SQL, Cloud Storage, self-hosted LiveKit), its sub-processors
(Anthropic, AssemblyAI, Google, email/SMTP, Stripe), local developer workstations that
hold secrets, and the ePHI data flows among them.

**Out of scope:** the clinicians' own environments and devices (their responsibility as
Covered Entities); end-client personal devices.

---

## 2. System description

- **Application:** Python / Flask + Flask-SocketIO (eventlet), server-rendered UI.
- **Hosting:** Google Cloud Run, region `us-central1`, single instance (`min=max=1`);
  TLS terminated at the Cloud Run edge.
- **Database:** Cloud SQL for PostgreSQL (US). Daily automated backups + 7-day
  point-in-time recovery (enabled 2026-07-26).
- **Real-time A/V:** self-hosted LiveKit (open source) on Google Cloud; media over
  WebRTC (DTLS-SRTP), signaling over WSS.
- **Live transcription:** browser streams session audio to AssemblyAI over WSS.
- **AI co-pilot / summaries / translation:** Anthropic Claude (server-side calls).
- **Billing:** Stripe Checkout (no card data stored by the app).
- **Identity:** OAuth / OpenID Connect (Google, Microsoft) for clinicians and clients;
  ECDSA public-key authentication for API clients.

---

## 3. ePHI inventory & data flows

| Data element | Where it lives | Protection at rest | In transit |
|---|---|---|---|
| Session chat / transcript text | Cloud SQL (`chat_messages.text`) | Field-encrypted (Fernet) + Cloud SQL AES-256 | TLS / WSS |
| Session summaries | Cloud SQL (`session_summaries`) | Field-encrypted + AES-256 | TLS |
| Co-pilot suggestion cards | Cloud SQL (`copilot_cards`) | Field-encrypted + AES-256 | TLS |
| Participant display names | Cloud SQL (`chat_messages`, `session_participants`) | Field-encrypted + AES-256 | TLS |
| Session labels (friendly names) | Cloud SQL (`therapy_sessions`) | Field-encrypted + AES-256 (HMAC lookup key) | TLS |
| Licensure state attestations | Cloud SQL (`session_state_certs`) | Cloud SQL AES-256 | TLS |
| Clinician email | Cloud SQL (`clinicians.email`) | Field-encrypted + AES-256 | TLS |
| Session audio | **Transcribed live; not recorded by default** | n/a unless recorded | WSS to AssemblyAI |
| Session recordings (opt-in only, all-party consent) | Cloud Storage | GCS AES-256 | TLS |
| Audit log | Cloud SQL (`audit_logs`) | Tamper-evident hash chain | TLS |

**Note on encryption strength:** Cloud SQL and Cloud Storage encrypt at rest with
**AES-256**. The application field-level layer uses **Fernet (AES-128-CBC + HMAC-SHA256)**
via `sqlalchemy-utils`. Both layers apply to the sensitive fields above.

---

## 4. Existing safeguards

### Technical (§164.312)
- **Access control:** OAuth/OIDC identity; ECDSA API auth; clinician-only transcript/
  recording downloads; waiting-room admission gated by therapist-presence heartbeat;
  licensure gate before client admission; **automatic logoff after 30 min idle**;
  session regeneration on login; unique user IDs per account.
- **Audit controls:** tamper-evident audit hash chain (`audit.py`) recording logins,
  consent, AI suggestions, risk/crisis alerts, transcript access; retained 6 years.
- **Integrity:** audit hash chaining; Fernet fields carry an HMAC.
- **Person/entity authentication:** federated identity (Google/Microsoft).
- **Transmission security:** TLS 1.2+ at the edge; **HSTS** in production; WSS/DTLS-SRTP
  for real-time media and transcription.
- **Application hardening:** enforced Content-Security-Policy (nonce'd scripts),
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`; CSRF synchronizer
  tokens; hardened session cookies (`HttpOnly`, `SameSite=Lax`, `Secure`); per-IP rate
  limiting; input validation; **PHI redaction in application logs**.
- **Secrets management:** Google Secret Manager (production); SOPS + Cloud KMS on
  developer laptops (no plaintext secrets at rest); fail-closed `SECRET_KEY`.
- **Data minimization:** no client account required; display names need not be real
  names; client-side data minimization.

### Administrative (§164.308)
- **Business Associate Agreements** signed with sub-processors (Google Cloud, Anthropic,
  AssemblyAI, email/SMTP provider), including no-training / limited-retention terms.
- Retention & disposal: clinical records ~6 years; audio recordings 30 days then deleted;
  audit logs 6 years — enforced by scheduled purge jobs (APScheduler).
- `[Security Officer / Privacy Officer formally designated? — record here]`

### Physical (§164.310)
- Infrastructure hosted in Google Cloud SOC-2 / ISO-27001 data centers (US).
- Developer-workstation controls: `[confirm full-disk encryption (BitLocker) is enabled]`.

---

## 5. Risk assessment

Risk = Likelihood × Impact after existing safeguards (residual risk).
Scale: Low / Moderate / High.

| # | Threat / vulnerability | Existing safeguards | Likelihood | Impact | Residual risk | Action |
|---|---|---|---|---|---|---|
| 1 | External unauthorized access to ePHI | Encryption (rest+transit), OAuth, CSP, rate limiting, WAF at edge | Low | High | **Low–Moderate** | Maintain; periodic pen test |
| 2 | Credential compromise (no app-layer MFA) | MFA delegated to Google/Microsoft IdP | Moderate | High | **Moderate** | Require/verify MFA at the IdP for clinician accounts |
| 3 | Workforce / insider misuse | Access controls + audit log | Low | High | **Moderate** | Pending workforce training + sanction policy (§6) |
| 4 | Lost/stolen developer device exposing secrets | SOPS-encrypted secrets vault | Low | Moderate | **Low** | Confirm full-disk encryption on all dev machines |
| 5 | Data loss / DB corruption | Cloud SQL daily backups + 7-day PITR | Low | High | **Low** | Test a restore; document DR runbook |
| 6 | Sub-processor breach (Anthropic/AssemblyAI/Google/email) | Signed BAAs, TLS, no-train terms | Low | High | **Low–Moderate** | Annual sub-processor review |
| 7 | Service unavailability (single `min=max=1` instance) | Cloud Run managed platform | Moderate | Moderate | **Moderate** | Document contingency/emergency-mode plan; consider redundancy |
| 8 | Injection / XSS / CSRF | Enforced CSP, CSRF tokens, input validation | Low | High | **Low** | Maintain; re-review CSP periodically |
| 9 | PHI leakage via logs | Root-logger redaction filter | Low | Moderate | **Low** | Periodic spot-check of production logs |
| 10 | Improper retention / disposal of ePHI | Scheduled purge jobs; documented retention | Low | Moderate | **Low** | Verify purge jobs run as intended |
| 11 | No formal risk-management program / policies | This document (in progress) | — | — | **Moderate** | Complete §6 deliverables |
| 12 | Unprepared breach response | — | — | High | **Moderate** | Adopt breach-notification procedure (§6) |

---

## 6. Risk-management plan (open items)

These are the remaining gaps. Most are administrative (policy/process), not technical.

| Item | Requirement | Owner | Target date | Status |
|---|---|---|---|---|
| Written policies & procedures (access mgmt, sanctions, incident response, etc.), retained 6 yrs | §164.316 | `[Security Officer]` | `[date]` | Open |
| Workforce security training + sanction policy | §164.308(a)(5) | `[ ]` | `[ ]` | Open |
| Contingency plan: data-backup plan (done), **disaster-recovery + emergency-mode runbook**, and a tested restore | §164.308(a)(7) | `[ ]` | `[ ]` | Partial (backups enabled) |
| Breach-notification procedure (60-day rule) | §164.400–414 | `[ ]` | `[ ]` | Open |
| BAA template to execute **with clinician customers** | §164.308(b), §164.314 | `[ ]` | `[ ]` | Open |
| Verify/require MFA at the identity provider | §164.308(a)(5)(ii)(D) | `[ ]` | `[ ]` | Open |
| Confirm full-disk encryption on developer workstations | §164.310(d) | `[ ]` | `[ ]` | Open |
| Periodic technical evaluation / penetration test | §164.308(a)(8) | `[ ]` | `[ ]` | Open |
| Consider third-party attestation (SOC 2 / HITRUST) to evidence to customers | (market/contractual) | `[ ]` | `[ ]` | Open |

---

## 7. Approval

| Role | Name | Signature | Date |
|---|---|---|---|
| HIPAA Security Officer | `[ ]` | | |
| Reviewed by (legal/compliance) | `[ ]` | | |

*Prepared as a working draft on 2026-07-26 from the system's implemented safeguards.
Not legal advice. Requires review and approval before use.*
