"""
tests/test_documents.py
-----------------------
Unit tests for documents.py — the pure summary renderers extracted from the app
monolith. They take a document object + a summary dict and touch nothing else,
so they can be exercised in isolation (no app, no DB, no Flask session).
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import io

import documents

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "fonts")

_SUMMARY = {
    "disclaimer": "Private to the clinician.",
    "clinical": "Client reports low mood and poor sleep.",
    "codes": [{"code": "F32.1", "label": "Moderate depressive episode", "source": "ICD-11"}],
    "codes_rationale": "Grounded from the session's flagged themes.",
    "copilot_cards": [{"type": "risk", "text": "Sleep disruption noted.", "code": ""}],
    "client_recap": "You spoke about a hard week; we agreed to try a sleep routine.",
}


def test_render_summary_pdf_writes_content():
    from fpdf import FPDF
    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.add_font("DejaVu", fname=os.path.join(_FONT_DIR, "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", fname=os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf"))
    pdf.add_page()

    documents.render_summary_pdf(pdf, _SUMMARY)

    out = bytes(pdf.output())
    assert out.startswith(b"%PDF")       # a valid PDF was produced
    assert len(out) > 800                 # non-trivial content was written


def test_render_summary_docx_writes_sections():
    from docx import Document
    doc = Document()

    documents.render_summary_docx(doc, _SUMMARY)

    text = "\n".join(p.text for p in doc.paragraphs)
    assert "ICD codes (billing reference)" in text
    assert "F32.1" in text
    assert "Co-pilot alerts (full record)" in text
    assert "Sleep disruption noted." in text
    # Round-trips to a real .docx file in memory.
    buf = io.BytesIO()
    doc.save(buf)
    assert buf.getbuffer().nbytes > 0


def test_render_summary_docx_handles_empty_summary():
    """Missing/empty keys must not raise — the record still renders."""
    from docx import Document
    doc = Document()
    documents.render_summary_docx(doc, {})
    text = "\n".join(p.text for p in doc.paragraphs)
    # ICD block is always emitted, even with no codes.
    assert "No ICD reference codes surfaced during this session." in text
