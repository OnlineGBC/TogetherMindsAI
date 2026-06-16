# Workforce Roles, Job Descriptions & ePHI Access Requirements

| | |
|---|---|
| **Document ID** | TM‑SEC‑JD‑001 |
| **Owner** | Security Officer |
| **Version** | 1.0 |
| **Status** | Draft — pending approval |
| **Last reviewed** | _[YYYY-MM-DD]_ |
| **Next review due** | _[YYYY-MM-DD — ≤12 months]_ |

> Fill the **[bracketed]** incumbent names/dates before approval. Roles are defined by function; in a small organization one person may hold several (see §6).

---

## 1. Purpose
Define, in writing, every workforce role that can access electronic protected health information (**ePHI**), the scope of that access, the HIPAA responsibilities of the role, and the **qualifications required** to hold it — so access is granted only to suitable, trained personnel on a minimum‑necessary basis.

## 2. Scope
- Applies to all **workforce members** — employees, contractors, and anyone else under TogetherMindsAI's direct control — who create, receive, maintain, transmit, or can access ePHI or the systems that hold it.
- **Out of scope:** *clinicians* (Covered Entities / their own workforce — TogetherMindsAI is their Business Associate) and *clients* (data subjects). They are not TogetherMindsAI workforce.

## 3. Regulatory basis
- **45 CFR §164.308(a)(2)** — Assigned Security Responsibility
- **45 CFR §164.308(a)(3)** — Workforce Security (authorization, clearance, termination)
- **45 CFR §164.308(a)(4)** — Information Access Management (minimum necessary)
- **45 CFR §164.308(a)(5)** — Security Awareness and Training

## 4. Requirements common to ALL roles with ePHI access
Before access is granted, and continuously thereafter, every workforce member must:
1. Sign a **confidentiality / acceptable‑use agreement** covering ePHI.
2. Complete **HIPAA Security & Privacy training** *before* receiving access, and at least **annually** thereafter (completion recorded).
3. Receive access on a **least‑privilege / minimum‑necessary** basis, provisioned through the access‑management process and **reviewed periodically**.
4. Use **unique credentials with MFA**; never share accounts.
5. Acknowledge the **Sanction Policy** — violations may result in disciplinary action up to termination.
6. (Where applicable / for privileged roles) pass a **background check** appropriate to the access level.
7. Have access **revoked immediately on role change or termination.**

## 5. Role definitions

### 5.1 HIPAA Security Officer *(required — §164.308(a)(2))*
- **Reports to:** Executive / Owner
- **Purpose:** Develop, implement, and maintain the information‑security program; ensure Security Rule compliance.
- **ePHI access scope:** Administrative/oversight access across in‑scope systems, limited to what security and audit duties require.
- **Key HIPAA responsibilities:** Lead risk assessments; own policy lifecycle (TM‑SEC‑PROC‑001); manage access authorization & reviews; lead incident response and breach assessment; oversee BAAs and vendor risk; run the training program; review audit logs.
- **Required qualifications:** Working knowledge of the HIPAA Security Rule; information‑security experience; ability to perform risk analysis; familiarity with cloud security (GCP IAM, encryption). **Preferred:** CISSP, HCISPP, CISM, or equivalent.

### 5.2 HIPAA Privacy Officer *(may be combined with the Security Officer)*
- **Reports to:** Executive / Owner
- **Purpose:** Ensure Privacy Rule compliance and handling of PHI uses/disclosures.
- **ePHI access scope:** As needed to investigate privacy matters and complaints.
- **Key HIPAA responsibilities:** Maintain privacy policies, consent/notice, and minimum‑necessary rules; handle individual rights and complaints; co‑own the breach‑notification process.
- **Required qualifications:** Working knowledge of the HIPAA Privacy Rule; healthcare‑privacy experience. **Preferred:** CHPC, HCISPP, or equivalent.

### 5.3 Software Engineer / Developer
- **Reports to:** Security Officer (for ePHI matters) / Engineering Lead
- **Purpose:** Build and maintain the application that processes ePHI.
- **ePHI access scope:** May access production data/database **only when required** for debugging or maintenance; minimum‑necessary; all access logged. No copying ePHI to local/unmanaged storage.
- **Key HIPAA responsibilities:** Secure coding (encryption, access control, input validation); follow change management; never disable safeguards; report incidents immediately.
- **Required qualifications:** Software‑engineering experience (Python/Flask, web/real‑time); secure‑development practices (e.g., OWASP); understanding of encryption and access control; completion of HIPAA + secure‑coding training.

### 5.4 Cloud / Infrastructure Administrator *(may be combined with 5.3)*
- **Reports to:** Security Officer / Engineering Lead
- **Purpose:** Operate the GCP infrastructure, deployments, secrets, and backups that store/transmit ePHI.
- **ePHI access scope:** Administrative access to ePHI‑bearing systems (database, backups, secret manager); minimum‑necessary; logged.
- **Key HIPAA responsibilities:** Configure and verify encryption (in transit & at rest), IAM/least‑privilege, audit logging, monitoring, patching, and backup/disaster‑recovery; manage secrets; support incident response.
- **Required qualifications:** Cloud infrastructure experience (GCP), IAM, networking, encryption, and backup/DR; security best practices; completion of HIPAA training.

> **Future roles:** any new role that can access ePHI (e.g., customer support, data analyst) **must be added here with its access scope and qualifications before access is granted.**

## 6. Role consolidation (small organization)
One individual may hold multiple roles above. The **Security Officer role is mandatory and must be a single named person.** Where staffing allows, separate the Approver of access from the person requesting it. Record current assignments below.

| Role | Incumbent | Effective date |
|---|---|---|
| Security Officer | _[name]_ | _[date]_ |
| Privacy Officer | _[name]_ | _[date]_ |
| Software Engineer / Developer | _[name]_ | _[date]_ |
| Cloud / Infrastructure Administrator | _[name]_ | _[date]_ |

## 7. Access provisioning & review
Access is granted, modified, and revoked per the Information Access Management & Access Control policy (TM‑SEC‑POL‑004), on the minimum‑necessary principle, and reviewed at least annually and on every role change or termination.

## 8. Acknowledgment
Each workforce member signs to confirm they have read this document, understand the responsibilities of their role, and agree to the common requirements in §4.

| Name | Role | Signature / commit | Date |
|---|---|---|---|
| _[name]_ | _[role]_ | _[ ]_ | _[date]_ |

---

### Approval
| Version | Approved by | Role | Date |
|---|---|---|---|
| 1.0 | _[name]_ | _[Security Officer]_ | _[YYYY-MM-DD]_ |
