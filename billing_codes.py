"""
billing_codes.py
-----------------
Deterministic lookups for the codes that go in the EHR note's "Billing code
considerations" section — CPT for the visit, Place-of-Service for the
telehealth billing rule that actually changes what the claim pays.

Flask-free and network-free on purpose, like ehr.py: every function here is a
plain lookup over facts the caller already has, so the exact result is
assertable in a test without a request or a database.

REFUSING BEATS GUESSING, same rule as the rest of this feature. A psychotherapy
CPT code needs a real session duration; with none, return None rather than
invent one. A Place-of-Service code needs a real home/not-home attestation;
with none, return None rather than assume "home" was answered.

TWO DIFFERENT KINDS OF CODE, ON PURPOSE. TMAI knows enough about a
clinician-led session (its mode, its length) to pick a psychotherapy CPT code
itself. It knows nothing about a patient's chief complaint, exam findings, or
medical decision-making, so it has no basis to pick an internist E&M code —
that list exists so a clinician can pick one by hand, not so TMAI can guess.
"""

PSYCHOTHERAPY_CPT = {
    "couple": {"code": "90847", "label": "Family psychotherapy (conjoint), with patient present"},
    "group": {"code": "90853", "label": "Group psychotherapy"},
}

# Individual ("solo") psychotherapy is billed by time. Bounds are inclusive on
# the low end, per the standard CPT time ranges; below the shortest range is
# not billable as psychotherapy at all.
_SOLO_TIME_BUCKETS = (
    (16, 37, "90832", "Psychotherapy, 30 minutes"),
    (38, 52, "90834", "Psychotherapy, 45 minutes"),
    (53, 10_000, "90837", "Psychotherapy, 60 minutes"),
)

# Standard internist/primary-care office-visit E&M codes, for a clinician to
# pick by hand — TMAI has no data to select one of these itself. Both scales
# (new vs. established patient) are level-by-time/complexity, per CPT.
INTERNIST_EM_CODES = (
    {"code": "99202", "label": "Office/outpatient visit, new patient — straightforward (15-29 min)"},
    {"code": "99203", "label": "Office/outpatient visit, new patient — low complexity (30-44 min)"},
    {"code": "99204", "label": "Office/outpatient visit, new patient — moderate complexity (45-59 min)"},
    {"code": "99205", "label": "Office/outpatient visit, new patient — high complexity (60-74 min)"},
    {"code": "99212", "label": "Office/outpatient visit, established patient — straightforward (10-19 min)"},
    {"code": "99213", "label": "Office/outpatient visit, established patient — low complexity (20-29 min)"},
    {"code": "99214", "label": "Office/outpatient visit, established patient — moderate complexity (30-39 min)"},
    {"code": "99215", "label": "Office/outpatient visit, established patient — high complexity (40-54 min)"},
)

_EM_CODES_BY_VALUE = {c["code"]: c for c in INTERNIST_EM_CODES}


def psychotherapy_cpt(mode: str, duration_minutes: "float | None") -> "dict | None":
    """The psychotherapy CPT code this session's own facts support, or None
    when there isn't enough to say so without guessing.

    `mode` is the session's own TherapySession.mode ("solo", "couple", or
    "group"). `duration_minutes` is the caller's best measurement of how long
    the session ran (see TogetherMindsAI._session_duration_minutes) — None
    when there is nothing to measure it from.
    """
    fixed = PSYCHOTHERAPY_CPT.get(mode)
    if fixed:
        return dict(fixed)
    if mode != "solo" or duration_minutes is None:
        return None
    for low, high, code, label in _SOLO_TIME_BUCKETS:
        if low <= duration_minutes <= high:
            return {"code": code, "label": label}
    return None  # shorter than the shortest billable bucket


def place_of_service(at_home: "bool | None") -> "dict | None":
    """The Place-of-Service code for a telehealth visit, from whether the
    patient attested they were at home. None when there is no attestation on
    file — silence is not "home", it's unknown."""
    if at_home is None:
        return None
    if at_home:
        return {"code": "10", "label": "Telehealth Provided in Patient's Home"}
    return {"code": "02", "label": "Telehealth Provided Other than in Patient's Home"}


def internist_em_code(code: str) -> "dict | None":
    """Look up one of INTERNIST_EM_CODES by its code, for the clinician's own
    hand-picked selection. None for anything not on the list — this is a fixed
    menu, not free text."""
    return dict(_EM_CODES_BY_VALUE[code]) if code in _EM_CODES_BY_VALUE else None
