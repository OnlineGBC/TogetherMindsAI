"""
clinical_reference.py
---------------------
ICD grounding for the therapist co-pilot.

The co-pilot's question/technique/observation cards come from the model's own
knowledge. This module adds an *authoritative* layer on top: a small curated
corpus of ICD-10/ICD-11 entries (with DSM-5-TR cross-references) plus a lexical
retriever. When the live transcript matches an entry, two things happen:

  1. The matched entries are formatted into a reference block that is injected
     into the advisor prompt, so the model's suggestions are *informed* by them.
  2. Deterministic "reference" cards are built straight from the corpus, so the
     ICD codes the therapist sees are read from the curated file — never written
     by the model, and therefore never hallucinated.

Design notes
------------
* Public-domain content only. ICD-10-CM codes/labels are CMS public domain;
  ICD-11 codes are WHO free reference; DSM cross-references name the disorder
  without reproducing any copyrighted criteria text. See the corpus file's
  license_note.
* Never raises into the co-pilot. A missing or malformed corpus, or any lookup
  error, degrades to "no reference material" — the session is never disrupted.
* Retrieval is deliberately lexical (substring keyword match) for v1: auditable,
  dependency-free, and good enough to surface the obvious presentations. The
  retrieve() interface is the seam where an embeddings backend can drop in later.
* This is reference material for a licensed clinician's private consideration —
  not a diagnosis and not a recommendation. The card text and prompt block both
  state that explicitly.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_CORPUS_PATH = os.path.join(os.path.dirname(__file__), "clinical_data", "icd_corpus.json")

# Minimum lexical score for an entry to surface as a reference card. A phrase
# (multi-word keyword) scores 2, a single-token keyword scores 1, so the default
# of 2 means "one strong phrase hit, or two distinct weaker hits" — conservative
# enough to avoid flagging on a single common word like "trauma".
_MIN_CARD_SCORE = 2

# How many reference cards to surface per turn. Kept low so the panel stays
# glanceable; dedupe upstream then prevents the same card recurring each turn.
MAX_REFERENCE_CARDS = 2

_corpus_cache = None


def _load_corpus() -> dict:
    """Load and cache the ICD corpus. Returns {} (and logs once) on any error."""
    global _corpus_cache
    if _corpus_cache is None:
        try:
            with open(_CORPUS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                raise ValueError("corpus missing 'entries' list")
            _corpus_cache = data
        except Exception as exc:
            logger.warning("Clinical reference corpus unavailable (%s); grounding disabled.", exc)
            _corpus_cache = {"entries": []}
    return _corpus_cache


def _score(text_lower: str, entry: dict) -> int:
    """Lexical score for one entry: phrase keyword = 2 points, single token = 1."""
    score = 0
    for kw in entry.get("keywords", []):
        k = kw.lower().strip()
        if k and k in text_lower:
            score += 2 if " " in k else 1
    return score


def retrieve(text: str, k: int = 3, min_score: int = 1) -> list:
    """Return up to `k` corpus entries most relevant to `text`, scored highest first.

    Each returned entry is the corpus dict with an added "_score". Entries scoring
    below `min_score` are dropped. Never raises — returns [] on any problem.
    """
    if not text or not text.strip():
        return []
    try:
        entries = _load_corpus().get("entries", [])
        lowered = text.lower()
        scored = []
        for entry in entries:
            s = _score(lowered, entry)
            if s >= min_score:
                scored.append({**entry, "_score": s})
        # Highest score first; ties keep corpus order (stable sort) for determinism.
        scored.sort(key=lambda e: e["_score"], reverse=True)
        return scored[:k]
    except Exception as exc:
        logger.warning("Clinical reference retrieval failed (%s); no reference material.", exc)
        return []


def format_reference_block(entries: list) -> str:
    """Format retrieved entries into a prompt block, or "" when there are none.

    Injected into the advisor prompt so the model's own cards are informed by the
    reference material. The model is told NOT to cite codes itself — the codes are
    surfaced separately by build_reference_cards.
    """
    if not entries:
        return ""
    lines = [
        "Reference material — ICD entries that may relate to the current presentation, "
        "for your private clinical consideration ONLY (these are not diagnoses, and a "
        "separate grounded layer cites the codes — do not output codes yourself):",
    ]
    for e in entries:
        codes = f"ICD-10 {e.get('icd10', '?')}"
        if e.get("icd11"):
            codes += f" · ICD-11 {e['icd11']}"
        if e.get("dsm_xref"):
            codes += f" · {e['dsm_xref']}"
        line = f"- {e.get('label', '?')} [{codes}]: {e.get('summary', '').strip()}"
        sq = e.get("screening_questions") or []
        if sq:
            line += f" Possible screen: \"{sq[0]}\""
        lines.append(line)
    lines.append(
        "Use these only to sharpen your question / technique / observation cards."
    )
    return "\n".join(lines)


def build_reference_cards(text: str, max_cards: int = MAX_REFERENCE_CARDS) -> list:
    """Build deterministic, grounded reference cards from the transcript.

    Codes are read straight from the curated corpus, so they can never be
    fabricated. Returns [] when nothing clears the relevance threshold.
    """
    entries = retrieve(text, k=max_cards, min_score=_MIN_CARD_SCORE)
    cards = []
    for e in entries:
        codes = f"ICD-10 {e.get('icd10', '?')}"
        if e.get("icd11"):
            codes += f" · ICD-11 {e['icd11']}"
        source = e.get("source", "ICD-10-CM (CMS, public domain)")
        if e.get("dsm_xref"):
            source += f" · cross-ref {e['dsm_xref']}"
        cards.append({
            "type": "reference",
            "text": (
                f"The conversation touches on features associated with {e.get('label', '?')}. "
                "Offered as reference for your assessment — not a diagnosis."
            ),
            "code": codes,                       # plain-text fallback
            "code_links": _code_links(e),        # clickable per-code links
            "source": source,
            "confidence": 0.5,
        })
    return cards


def _code_links(entry: dict) -> list:
    """Build clickable per-code links for one entry.

    The ICD-10 link is a THIRD-PARTY lookup (icd10data.com) — flagged so the UI
    can label it as such — and the ICD-11 link is the official WHO browser. The
    DSM cross-reference is intentionally NOT linked (no free canonical page); it
    stays plain text in the card's source line.
    """
    links = []
    if entry.get("icd10_url"):
        links.append({
            "label": f"ICD-10 {entry.get('icd10', '?')}",
            "url": entry["icd10_url"],
            "third_party": True,
        })
    if entry.get("icd11") and entry.get("icd11_url"):
        links.append({
            "label": f"ICD-11 {entry['icd11']}",
            "url": entry["icd11_url"],
            "third_party": False,
        })
    return links
