"""
tests/test_billing_codes.py
----------------------------
Pure lookups for the EHR note's "Billing code considerations" section.

No Flask, no database, no network — same reason ehr.py's pure functions are
tested this way: the exact result is assertable without any of that, and
REFUSING BEATS GUESSING is the thing under the microscope here. A wrong CPT
or Place-of-Service code changes what an insurance claim pays, so "we don't
know" has to come back as None, never as a best guess.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import billing_codes


# ---------------------------------------------------------------------------
# Psychotherapy CPT
# ---------------------------------------------------------------------------

def test_couple_sessions_get_the_conjoint_code_regardless_of_duration():
    out = billing_codes.psychotherapy_cpt("couple", duration_minutes=5)
    assert out == {"code": "90847",
                   "label": "Family psychotherapy (conjoint), with patient present"}


def test_group_sessions_get_the_group_code():
    out = billing_codes.psychotherapy_cpt("group", duration_minutes=None)
    assert out["code"] == "90853"


def test_solo_duration_picks_the_matching_time_bucket():
    assert billing_codes.psychotherapy_cpt("solo", 20)["code"] == "90832"
    assert billing_codes.psychotherapy_cpt("solo", 45)["code"] == "90834"
    assert billing_codes.psychotherapy_cpt("solo", 60)["code"] == "90837"


def test_solo_duration_at_bucket_edges():
    assert billing_codes.psychotherapy_cpt("solo", 16)["code"] == "90832"   # low edge
    assert billing_codes.psychotherapy_cpt("solo", 37)["code"] == "90832"   # high edge
    assert billing_codes.psychotherapy_cpt("solo", 38)["code"] == "90834"


def test_solo_with_no_duration_is_refused_not_guessed():
    assert billing_codes.psychotherapy_cpt("solo", None) is None


def test_solo_shorter_than_the_shortest_billable_bucket_is_refused():
    assert billing_codes.psychotherapy_cpt("solo", 5) is None


# ---------------------------------------------------------------------------
# Place of Service
# ---------------------------------------------------------------------------

def test_home_is_pos_10():
    out = billing_codes.place_of_service(True)
    assert out == {"code": "10", "label": "Telehealth Provided in Patient's Home"}


def test_not_home_is_pos_02():
    out = billing_codes.place_of_service(False)
    assert out["code"] == "02"


def test_no_attestation_is_refused_not_assumed_home():
    """Silence is not 'home' — it's unknown, and unknown must not become a
    guessed code on a claim."""
    assert billing_codes.place_of_service(None) is None


# ---------------------------------------------------------------------------
# Internist E&M — a fixed menu, not a TMAI-picked value
# ---------------------------------------------------------------------------

def test_a_listed_em_code_resolves():
    out = billing_codes.internist_em_code("99213")
    assert out["code"] == "99213"
    assert "established patient" in out["label"]


def test_an_unlisted_code_is_refused():
    assert billing_codes.internist_em_code("00000") is None


def test_an_empty_code_is_refused():
    assert billing_codes.internist_em_code("") is None


def test_the_em_menu_covers_new_and_established_patients():
    codes = {c["code"] for c in billing_codes.INTERNIST_EM_CODES}
    assert {"99202", "99203", "99204", "99205"} <= codes   # new patient
    assert {"99212", "99213", "99214", "99215"} <= codes   # established patient
