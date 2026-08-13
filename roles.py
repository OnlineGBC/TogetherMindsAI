"""
roles.py
--------
Who the account holder is, and what that lets them reach.

Role answers "what kind of user is this". Plan answers "have they paid". Both
apply: a capability is available when the ROLE offers it AND (it is free, or the
account is paid). Neither switch alone decides.

Hardcoded on purpose — these are product decisions, not user data.

Flask-free so every rule here is directly testable. Step 1 of the roles work adds
this table and the account field only; nothing reads it for gating yet.
"""

# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

LIVE_AV   = "live_av"        # watch/listen in real time
CHAT      = "chat"           # the text conversation
TRANSCRIPT = "transcript"    # the written record of what was said
SAFETY    = "safety_alerts"  # risk/crisis cards — never sold, but not every role has words to read
AI        = "ai"             # co-pilot suggestions, answers, and the AI recap
ICD       = "icd_codes"      # ICD / billing code support
RECORDING = "recording"      # audio/video capture and storage

ALL_CAPABILITIES = (LIVE_AV, CHAT, TRANSCRIPT, SAFETY, AI, ICD, RECORDING)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

PSYCHOTHERAPIST = "psychotherapist"
HYPNOTHERAPIST  = "hypnotherapist"
CAREGIVER       = "caregiver"

ROLES = {
    PSYCHOTHERAPIST: {
        "label": "Psychologist / Psychotherapist",
        "blurb": "Licensed clinical work with clients.",
        "free":  {LIVE_AV, CHAT, TRANSCRIPT, SAFETY},
        "paid":  {AI, ICD, RECORDING},
        "licence_check": True,     # must certify they may practise in the client's state
        "clinical": True,          # may use "clinician" / "clinical record" wording
        # How the co-pilot is told to think about this practitioner. Without it
        # every role got the same talking-therapy prompt, so a hypnotherapist was
        # offered person-centred moves ("invite them to say more") mid-induction.
        # Required of every role — see tests/test_copilot_role_prompt.py.
        "copilot_trained_in":
            "trained in talking therapies (CBT, ACT, IFS, person-centred)",
        "copilot_fits":
            "questions, reflections and named talking-therapy techniques",
    },
    HYPNOTHERAPIST: {
        "label": "Hypnotherapist / hypnotic coach",
        "blurb": "Hypnotherapy and coaching. Not licensed health care.",
        "free":  {LIVE_AV, CHAT, TRANSCRIPT, SAFETY},
        "paid":  {AI, RECORDING},  # deliberately no ICD — coding is clinical
        "licence_check": False,
        "clinical": False,
        "copilot_trained_in":
            "trained in hypnotherapy and coaching — inductions, suggestion, "
            "deepeners, anchoring, motivational and goal-focused work",
        "copilot_fits":
            "what a symptom or habit does for the person, their triggers, "
            "readiness for trance, suggestion wording, and goal-focused moves. "
            "Do NOT default to open-ended exploration when they are working "
            "hypnotically",
    },
    CAREGIVER: {
        "label": "Nurse / parent / caregiver",
        "blurb": "Watching over a baby or a patient.",
        # Live watching is the free tier and the whole point of the role. There is
        # no chat and no transcript here, so there are also no safety alerts —
        # those read the words in a session, and this role has none.
        "free":  {LIVE_AV},
        "paid":  {RECORDING},
        "licence_check": False,
        "clinical": False,
        # This role has no chat and no transcript, so the co-pilot has nothing to
        # read. Present for completeness, and so adding words later needs no
        # change here.
        "copilot_trained_in": "watching over someone, not running a therapy session",
        "copilot_fits": "plain, practical notes about what they are seeing",
    },
}

# An account with no role yet behaves as a psychotherapist, which is exactly how
# the app behaved before roles existed. So a missing role can never quietly take
# something away. Step 3 makes every account choose one at next login.
DEFAULT_ROLE = PSYCHOTHERAPIST


def is_valid(role: str) -> bool:
    return role in ROLES


def role_of(clinician) -> str:
    """The account's role, falling back to the default for older/unset accounts."""
    role = getattr(clinician, "role", None) if clinician is not None else None
    return role if is_valid(role) else DEFAULT_ROLE


def spec(role: str) -> dict:
    return ROLES.get(role) or ROLES[DEFAULT_ROLE]


def capabilities(role: str, paid: bool) -> set:
    """Everything this role can reach at this payment level."""
    s = spec(role)
    return set(s["free"]) | (set(s["paid"]) if paid else set())


def allows(role: str, capability: str, paid: bool) -> bool:
    """Whether the role grants `capability` at this payment level."""
    return capability in capabilities(role, paid)


def sells(role: str, capability: str) -> bool:
    """Whether paying would ever unlock this capability for this role. Used to
    decide if an upgrade prompt makes sense, or if the answer is a flat no."""
    return capability in spec(role)["paid"]


def needs_licence_check(role: str) -> bool:
    """Whether this role must certify they may practise in the client's state."""
    return bool(spec(role)["licence_check"])


def is_clinical(role: str) -> bool:
    """Whether this role may be described in clinical terms ("clinician",
    "clinical record", "professional care"). False for coaches and caregivers."""
    return bool(spec(role)["clinical"])


# ---------------------------------------------------------------------------
# Wording. One table, read by every screen, so the language cannot drift apart.
# A word only appears here when it would be WRONG for some role — "session" is
# right for everyone, so it is not here.
# ---------------------------------------------------------------------------

_CRISIS = ("In a crisis, contact your {practitioner}; if they are unavailable")

WORDING = {
    PSYCHOTHERAPIST: {
        "practitioner": "clinician",
        "practitioner_plural": "clinicians",
        "console_title": "Therapist Co-Pilot",
        "record": "clinical record",
        "service": "professional care",
        "attestation": ("I am a licensed professional. The AI co-pilot is an assistive "
                        "aid only — it does not diagnose, treat, or replace my clinical "
                        "judgement, and I remain responsible for the session."),
        "disclaimer_lead": ("TogetherMindsAI is an AI assistant supporting your clinician"),
        "disclaimer_rest": ("not a replacement for their professional care. "
                            "In a crisis, contact your clinician; if they are unavailable"),
    },
    HYPNOTHERAPIST: {
        "practitioner": "practitioner",
        "practitioner_plural": "practitioners",
        "console_title": "Session Co-Pilot",
        "record": "session notes",
        "service": "their service",
        "attestation": ("I am a qualified practitioner. The AI co-pilot is an assistive "
                        "aid only — it does not diagnose or treat, and I remain "
                        "responsible for the session."),
        "disclaimer_lead": ("TogetherMindsAI is an AI assistant supporting your practitioner"),
        "disclaimer_rest": ("it is not mental-health care and does not replace it. "
                            "In a crisis"),
    },
    CAREGIVER: {
        "practitioner": "caregiver",
        "practitioner_plural": "caregivers",
        "console_title": "Monitor",
        "record": "recording",
        "service": "care",
        "attestation": ("I confirm I am authorised to record this person. I am their "
                        "parent or guardian, or I have their permission."),
        "disclaimer_lead": ("TogetherMindsAI helps you watch and record"),
        "disclaimer_rest": ("it is not medical advice and does not replace a doctor. "
                            "In an emergency, call 911"),
    },
}


def words(role: str) -> dict:
    """The wording for this role, falling back to the clinical set."""
    return WORDING.get(role) or WORDING[DEFAULT_ROLE]


def price_key(role: str) -> str:
    """Which config price ID sells this role's paid plan. The two clinical roles
    share one $16 price — the difference between them is ICD codes and the licence
    check, not the money."""
    return ("STRIPE_PRICE_CAREGIVER" if role == CAREGIVER
            else "STRIPE_PRICE_CLINICAL")


def price_label(role: str) -> str:
    """What the paid plan costs, for display."""
    return "$9.99" if role == CAREGIVER else "$16"


def paid_features(role: str) -> list:
    """Plain-language list of what paying unlocks, for the plans page."""
    return {
        PSYCHOTHERAPIST: ["AI co-pilot suggestions",
                          "AI session recap",
                          "ICD and billing codes",
                          "Audio and video recording"],
        HYPNOTHERAPIST:  ["AI co-pilot suggestions",
                          "AI session recap",
                          "Audio and video recording"],
        CAREGIVER:       ["Audio and video recording",
                          "40 hours a month",
                          "Recordings kept 30 days"],
    }.get(role, [])


def free_features(role: str) -> list:
    """Plain-language list of what the free tier gives, for the plans page."""
    return {
        PSYCHOTHERAPIST: ["Live audio and video",
                          "Guided reflections chat",
                          "Full session transcript",
                          "Safety and risk alerts"],
        HYPNOTHERAPIST:  ["Live audio and video",
                          "Guided reflections chat",
                          "Full session transcript",
                          "Safety and risk alerts"],
        CAREGIVER:       ["Live audio and video, unlimited"],
    }.get(role, [])


def choices() -> list:
    """(value, label, blurb) for the sign-up picker, in display order."""
    return [(r, ROLES[r]["label"], ROLES[r]["blurb"])
            for r in (PSYCHOTHERAPIST, HYPNOTHERAPIST, CAREGIVER)]


def copilot_framing(role: str) -> dict:
    """How the co-pilot should think about this practitioner.

    Every role must supply both keys. A new role that forgets them would silently
    fall back to the talking-therapy wording — which is the bug this replaced —
    so the values are read strictly and a missing one raises here rather than
    quietly mis-advising someone mid-session.
    """
    s = spec(role)
    try:
        return {"trained_in": s["copilot_trained_in"], "fits": s["copilot_fits"]}
    except KeyError as exc:
        raise KeyError(
            f"role {role!r} is missing {exc} — every role must say how the "
            f"co-pilot should think about it (see roles.ROLES)") from exc
