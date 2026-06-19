"""
tests/test_clinical_reference.py
--------------------------------
Tests for ICD grounding of the therapist co-pilot:

  - clinical_reference.retrieve: matches presentations, ignores logistics, thresholds
  - clinical_reference.build_reference_cards: grounded cards, real codes only, silence
  - clinical_reference.format_reference_block: prompt block content + framing
  - corpus integrity: every card's code is read from the curated file (never fabricated)
"""

import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Keep the lexical tests below on the keyword path (and never download a model in
# CI). The semantic tests further down inject a fake embedder directly.
os.environ.setdefault("EMBEDDING_ENABLED", "false")

import numpy as np
from unittest.mock import patch

import clinical_reference as cref


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------

def test_retrieve_matches_anxiety_presentation():
    hits = cref.retrieve("Client: I feel so anxious and I worry about everything, I can't relax")
    labels = [h["label"] for h in hits]
    assert "Generalized anxiety disorder" in labels


def test_retrieve_ranks_by_score():
    # Strong, repeated anxiety language should outrank a single incidental token.
    hits = cref.retrieve("I'm anxious, worrying constantly, on edge and can't relax. Also tired.")
    assert hits[0]["label"] == "Generalized anxiety disorder"
    assert hits[0]["_score"] >= hits[-1]["_score"]


def test_retrieve_empty_on_logistical_text():
    assert cref.retrieve("Hi, thanks, see you next week. One sec.") == []


def test_retrieve_empty_string():
    assert cref.retrieve("   ") == []


def test_retrieve_min_score_filters_weak_single_hits():
    # A single common single-token keyword ("trauma") scores 1 — below the card
    # threshold of 2, so it must not surface as a reference card.
    cards = cref.build_reference_cards("we talked about trauma briefly")
    assert cards == []


# ---------------------------------------------------------------------------
# build_reference_cards
# ---------------------------------------------------------------------------

def test_build_reference_cards_grounded_fields():
    cards = cref.build_reference_cards(
        "I keep having flashbacks and nightmares, I feel triggered and hypervigilant"
    )
    assert cards, "expected a PTSD reference card"
    card = cards[0]
    assert card["type"] == "reference"
    assert "F43.10" in card["code"]                 # real ICD-10 code from the corpus
    assert "DSM-5-TR" in card["source"]             # DSM cross-reference present
    assert "not a diagnosis" in card["text"].lower()


def test_build_reference_cards_silent_on_benign():
    assert cref.build_reference_cards("I had a pleasant walk in the park today") == []


def test_reference_card_code_links():
    """Reference cards carry clickable per-code links: ICD-10 → third-party lookup
    (flagged), ICD-11 → official WHO browser (not flagged), DSM left unlinked."""
    cards = cref.build_reference_cards(
        "I keep having flashbacks and nightmares, I feel triggered and hypervigilant"
    )
    assert cards
    links = cards[0]["code_links"]
    by_label = {l["label"].split()[0]: l for l in links}

    icd10 = by_label["ICD-10"]
    assert icd10["third_party"] is True
    assert "icd10data.com" in icd10["url"]

    icd11 = by_label["ICD-11"]
    assert icd11["third_party"] is False
    assert "icd.who.int" in icd11["url"]

    # The DSM cross-reference is text-only — it must never appear as a link.
    assert all("dsm" not in l["url"].lower() for l in links)


def test_build_reference_cards_caps_count():
    text = ("I'm depressed and hopeless, also anxious and worrying, can't sleep, "
            "drinking too much, and having panic attacks")
    cards = cref.build_reference_cards(text)
    assert len(cards) <= cref.MAX_REFERENCE_CARDS


# ---------------------------------------------------------------------------
# format_reference_block
# ---------------------------------------------------------------------------

def test_format_reference_block_empty_when_no_entries():
    assert cref.format_reference_block([]) == ""


def test_format_reference_block_frames_as_non_diagnostic():
    block = cref.format_reference_block(cref.retrieve("I feel anxious and worry all the time"))
    assert block
    assert "not diagnoses" in block.lower()
    assert "F41.1" in block                          # GAD ICD-10 surfaced in the block


# ---------------------------------------------------------------------------
# corpus integrity — codes must never be fabricated
# ---------------------------------------------------------------------------

def test_corpus_entries_have_required_fields():
    entries = cref._load_corpus().get("entries", [])
    assert entries, "corpus should load with entries"
    for e in entries:
        for field in ("id", "label", "icd10", "dsm_xref", "summary", "keywords", "source"):
            assert e.get(field), f"entry {e.get('id')} missing {field}"


def test_corpus_entries_have_valid_link_urls():
    """Every entry links ICD-10 to a third-party lookup and ICD-11 to a WHO
    per-code deep link (…/browse/…#<FoundationEntityId>)."""
    for e in cref._load_corpus().get("entries", []):
        assert e.get("icd10_url", "").startswith("https://"), f"{e.get('id')} bad icd10_url"
        icd11_url = e.get("icd11_url", "")
        assert icd11_url.startswith("https://icd.who.int/browse"), f"{e.get('id')} icd11_url not WHO browser"
        assert re.search(r"#\d+$", icd11_url), f"{e.get('id')} icd11_url missing #<entityId> deep link"


def test_card_codes_come_from_corpus():
    # Every code a card can emit must be an icd10 value present in the corpus —
    # proves the code is read from the curated file, not written by the model.
    valid = {e["icd10"] for e in cref._load_corpus().get("entries", [])}
    cards = cref.build_reference_cards(
        "I'm so depressed and hopeless, nothing matters and I have no energy"
    )
    assert cards
    for c in cards:
        icd10_part = c["code"].split(" · ")[0].replace("ICD-10 ", "")
        assert icd10_part in valid


# ---------------------------------------------------------------------------
# Semantic retrieval (local embeddings) — mocked so no model is downloaded
# ---------------------------------------------------------------------------

def _v(*xs):
    return np.array(xs, dtype=float)


def test_retrieve_semantic_matches_by_meaning():
    """Cosine match surfaces the right disorder even when the query shares no
    keywords with the entry — the recall the keyword matcher couldn't give."""
    adj = {"id": "adjustment", "label": "Adjustment disorder", "icd10": "F43.2"}
    gad = {"id": "gad", "label": "Generalized anxiety disorder", "icd10": "F41.1"}
    corpus = [(adj, _v(1, 0, 0)), (gad, _v(0, 1, 0))]
    with patch.object(cref, "_get_corpus_vecs", return_value=corpus), \
         patch.object(cref, "_embed", return_value=[_v(0.95, 0.05, 0)]):
        hits = cref._retrieve_semantic("I lost my job and can't pay rent", k=2, min_sim=0.58)
    assert hits[0]["label"] == "Adjustment disorder"
    assert "_sim" in hits[0]


def test_retrieve_semantic_threshold_rejects_weak_match():
    """A below-threshold cosine (benign chatter) yields no card."""
    adj = {"id": "adjustment", "label": "Adjustment disorder", "icd10": "F43.2"}
    with patch.object(cref, "_get_corpus_vecs", return_value=[(adj, _v(1, 0, 0))]), \
         patch.object(cref, "_embed", return_value=[_v(0.3, 0.95, 0)]):   # cosine ~0.30
        assert cref._retrieve_semantic("what time is our appointment", k=2, min_sim=0.58) == []


def test_retrieve_prefers_semantic_when_available():
    adj = {"id": "adjustment", "label": "Adjustment disorder", "icd10": "F43.2"}
    with patch.object(cref, "_get_corpus_vecs", return_value=[(adj, _v(1, 0, 0))]), \
         patch.object(cref, "_embed", return_value=[_v(1, 0, 0)]):
        hits = cref.retrieve("anything at all", k=1, min_sim=0.58)
    assert hits[0]["label"] == "Adjustment disorder"
    assert "_sim" in hits[0]          # came from the semantic path


def test_retrieve_falls_back_to_lexical_when_embeddings_unavailable():
    """When the model is unavailable (_get_corpus_vecs → None), retrieve degrades
    to keyword matching rather than returning nothing."""
    with patch.object(cref, "_get_corpus_vecs", return_value=None):
        hits = cref.retrieve("I feel so anxious and worry about everything, I can't relax")
    assert "Generalized anxiety disorder" in [h["label"] for h in hits]
    assert "_score" in hits[0]        # lexical entries carry _score, not _sim
