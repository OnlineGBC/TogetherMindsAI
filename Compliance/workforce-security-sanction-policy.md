# Workforce Security & Sanction Policy

| | |
|---|---|
| **Document ID** | TM‑SEC‑POL‑003 |
| **Owner** | _[Security Officer]_ |
| **Version** | 0.1 |
| **Status** | Draft |
| **Last reviewed** | _[YYYY-MM-DD]_ |
| **Next review due** | _[YYYY-MM-DD — ≤12 months]_ |
| **HIPAA citation(s)** | 45 CFR §164.308(a)(3); §164.308(a)(1)(ii)(C); §164.308(a)(4) |

## 1. Purpose
Ensure every workforce member who can access ePHI is appropriately **authorized, screened, trained, and supervised**; that access is **removed promptly** when no longer needed; and that violations of security/privacy policies are addressed through **consistent, documented sanctions**.

## 2. Scope
All TogetherMindsAI workforce (employees, contractors) with access to ePHI or the systems that store or transmit it. **Out of scope:** clinicians (Covered Entities) and clients (data subjects) — they are not TogetherMindsAI workforce.

## 3. Policy statements
1. Access to ePHI is granted **only after** authorization, a signed confidentiality/acceptable‑use agreement, and completed HIPAA training (per TM‑SEC‑JD‑001).
2. Access follows **least‑privilege / minimum‑necessary** and is documented.
3. **Privileged roles** (Security Officer, Cloud/Infra Admin) require a background check appropriate to the access level before access is granted.
4. Workforce activity involving ePHI is **supervised and audit‑logged**.
5. Access is **reviewed at least annually** and on any role change.
6. Access is **revoked immediately** on termination or role change (target: same business day).
7. Violations are **investigated and sanctioned consistently**, regardless of the member's role or seniority.
8. All sanctions are **documented and retained** (6 years).

## 4. Procedures / controls

### 4.1 Authorization & onboarding
- Security Officer authorizes access based on the role definition (TM‑SEC‑JD‑001).
- Member signs the confidentiality / acceptable‑use agreement.
- Member completes HIPAA training **before** access is granted.
- Access is provisioned least‑privilege via the Access Management policy (TM‑SEC‑POL‑004); **unique credentials + MFA**.

### 4.2 Workforce clearance (screening)
- A background check appropriate to the role is completed for privileged positions before access is granted, where legally permitted.

### 4.3 Supervision & ongoing review
- ePHI activity is logged (Audit Controls, TM‑SEC‑POL‑009).
- Access is reviewed at least annually; excess or stale access is corrected promptly.

### 4.4 Termination / role change
- On notice of termination or role change, the Security Officer / Admin **revokes all access** — accounts, API keys, OAuth sessions, devices — target **same business day**.
- Recover or wipe issued devices; **rotate any shared secrets** the member could have known.
- Record the revocation (what, when, by whom).

### 4.5 Sanctions process
1. A reported or discovered violation is escalated to the Security Officer.
2. The Security Officer investigates and **documents** the findings.
3. A sanction is applied per the tiers in §7, **consistently** across roles.
4. The action is recorded in the member's file and retained.
5. Contributing controls are reviewed to prevent recurrence; if ePHI was exposed, the **Breach Notification** process (TM‑SEC‑POL‑012) is triggered.

## 5. Roles & responsibilities
- **Security Officer:** authorize/review access; investigate violations; decide and document sanctions; maintain records.
- **Cloud/Infra Admin:** execute provisioning and immediate de‑provisioning.
- **Owner/Manager:** report issues; support consistent enforcement.
- **All workforce:** comply with policy; report suspected violations and incidents.

## 6. HIPAA mapping
| Requirement | How this policy satisfies it |
|---|---|
| §164.308(a)(3)(ii)(A) Authorization & Supervision | §4.1, §4.3 |
| §164.308(a)(3)(ii)(B) Workforce Clearance | §4.2 |
| §164.308(a)(3)(ii)(C) Termination Procedures | §4.4 |
| §164.308(a)(1)(ii)(C) Sanction Policy | §4.5, §7 |
| §164.308(a)(4) Information Access Management | §4.1 (via TM‑SEC‑POL‑004) |

## 7. Enforcement & sanctions
Sanctions are applied by severity, intent, and recurrence — and consistently across all roles:

| Tier | Example | Typical sanction |
|---|---|---|
| **1 — Minor / unintentional** | First‑time minor lapse, no PHI exposure | Verbal warning + retraining |
| **2 — Moderate** | Repeated lapses; negligent handling; limited PHI exposure | Written warning + mandatory retraining + access review |
| **3 — Serious** | Deliberate unauthorized access; significant exposure; credential sharing | Access suspension + formal disciplinary action |
| **4 — Severe** | Intentional misuse/theft of PHI; malicious acts | Termination; referral to authorities; breach process |

All sanctions are documented; repeat violations escalate to the next tier.

## 8. Exceptions
Time‑bound exceptions require Security Officer approval with documented justification, compensating controls, and an expiry/review date.

## 9. Related documents
- `workforce-roles-ephi-access.md` (TM‑SEC‑JD‑001)
- TM‑SEC‑POL‑004 — Information Access Management & Access Control
- TM‑SEC‑POL‑005 — Security Awareness & Training
- TM‑SEC‑POL‑009 — Audit Controls, Logging & Monitoring
- TM‑SEC‑POL‑012 — Breach Notification
- `security-policy-management-process.md` (TM‑SEC‑PROC‑001)

## 10. Review history
| Version | Date | Reviewer | Change | Approver |
|---|---|---|---|---|
| 0.1 | _[date]_ | _[name]_ | Initial draft | — |

## Approval
| Version | Approved by | Role | Date |
|---|---|---|---|
| _[x.y]_ | _[name]_ | _[Security Officer]_ | _[YYYY-MM-DD]_ |
