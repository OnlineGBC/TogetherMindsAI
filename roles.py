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
    },
    HYPNOTHERAPIST: {
        "label": "Hypnotherapist / hypnotic coach",
        "blurb": "Hypnotherapy and coaching. Not licensed health care.",
        "free":  {LIVE_AV, CHAT, TRANSCRIPT, SAFETY},
        "paid":  {AI, RECORDING},  # deliberately no ICD — coding is clinical
        "licence_check": False,
        "clinical": False,
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
