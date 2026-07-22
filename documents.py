"""
documents.py
------------
Document rendering for downloadable session records.

Pure formatting helpers: each takes an already-created document object (an fpdf
``FPDF`` or a python-docx ``Document``) plus a plain summary ``dict`` and writes
the clinician summary onto it. They touch no database, no Flask session, and no
shared state — so they are safe to unit-test in isolation and safe to call from
the transcript builders in the main app.

The expected ``summary`` shape (all keys optional):
    {
      "disclaimer":      str,
      "clinical":        str,
      "codes":           [{"code", "label", "source"}],
      "codes_rationale": str,
      "copilot_cards":   [{"type", "text", "code"}],
      "client_recap":    str,
    }
"""

_CARD_LABEL = {
    "risk": "Risk", "reference": "Reference",
    "suggestion": "Suggestion", "observation": "Observation",
}


def render_summary_pdf(pdf, summary: dict) -> None:
    """Prepend the therapist-only summary to the PDF, above the transcript."""
    from fpdf.enums import XPos, YPos

    # multi_cell defaults to new_x=RIGHT, which leaves the cursor at the right
    # margin and makes the NEXT multi_cell raise "not enough horizontal space".
    # Return to the left margin after every block.
    mc = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}

    pdf.set_font("DejaVu", "B", 14)
    pdf.set_text_color(146, 39, 15)
    pdf.cell(0, 9, "Clinician Summary — Private (therapist only)",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("DejaVu", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5, summary.get("disclaimer", ""), **mc)
    pdf.ln(2)

    def _section(title, body):
        if not body:
            return
        pdf.set_font("DejaVu", "B", 11)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 6, title, **mc)
        pdf.set_font("DejaVu", "", 10)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(0, 5, body, **mc)
        pdf.ln(2)

    _section("Clinical summary",
             summary.get("clinical") or "(AI narrative unavailable — see transcript below.)")

    # ICD codes — always rendered (grounded data), even if the narrative failed.
    pdf.set_font("DejaVu", "B", 11)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 6, "ICD codes (billing reference)", **mc)
    pdf.set_font("DejaVu", "", 10)
    pdf.set_text_color(30, 30, 30)
    codes = summary.get("codes") or []
    if codes:
        for c in codes:
            label = f"{c['label']} — " if c.get("label") else ""
            pdf.multi_cell(0, 5, f"• {label}{c.get('code', '')}  ({c.get('source', '')})", **mc)
    else:
        pdf.multi_cell(0, 5, "No ICD reference codes surfaced during this session.", **mc)
    if summary.get("codes_rationale"):
        pdf.ln(1)
        pdf.multi_cell(0, 5, summary["codes_rationale"], **mc)
    pdf.ln(2)

    # Co-pilot alerts — the full saved record (incl. any not shown live).
    cards = summary.get("copilot_cards") or []
    if cards:
        pdf.set_font("DejaVu", "B", 11)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 6, "Co-pilot alerts (full record)", **mc)
        pdf.set_font("DejaVu", "", 10)
        pdf.set_text_color(30, 30, 30)
        for c in cards:
            lbl = _CARD_LABEL.get(c.get("type"), (c.get("type") or "Note").title())
            code = f"  [{c['code']}]" if c.get("code") else ""
            pdf.multi_cell(0, 5, f"• [{lbl}] {c.get('text', '')}{code}", **mc)
        pdf.ln(2)

    _section("Client-facing draft — share only at your discretion "
             "(the client cannot see this unless you give it to them)",
             summary.get("client_recap"))

    pdf.set_draw_color(200, 200, 200)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(6)
    pdf.set_font("DejaVu", "B", 13)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 8, "Transcript", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)


def render_summary_docx(doc, summary: dict) -> None:
    """Prepend the therapist-only summary to the DOCX, above the transcript."""
    from docx.shared import Pt, RGBColor

    h = doc.add_heading("Clinician Summary — Private (therapist only)", level=2)
    h.runs[0].font.color.rgb = RGBColor(0x92, 0x27, 0x0F)
    disc = doc.add_paragraph()
    run = disc.add_run(summary.get("disclaimer", ""))
    run.italic = True
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x78, 0x78, 0x78)

    def _section(title, body):
        if not body:
            return
        p = doc.add_paragraph()
        p.add_run(title).bold = True
        doc.add_paragraph(body)

    _section("Clinical summary",
             summary.get("clinical") or "(AI narrative unavailable — see transcript below.)")

    p = doc.add_paragraph()
    p.add_run("ICD codes (billing reference)").bold = True
    codes = summary.get("codes") or []
    if codes:
        for c in codes:
            label = f"{c['label']} — " if c.get("label") else ""
            doc.add_paragraph(f"• {label}{c.get('code', '')}  ({c.get('source', '')})")
    else:
        doc.add_paragraph("No ICD reference codes surfaced during this session.")
    if summary.get("codes_rationale"):
        doc.add_paragraph(summary["codes_rationale"])

    # Co-pilot alerts — the full saved record (incl. any not shown live).
    cards = summary.get("copilot_cards") or []
    if cards:
        p = doc.add_paragraph()
        p.add_run("Co-pilot alerts (full record)").bold = True
        for c in cards:
            lbl = _CARD_LABEL.get(c.get("type"), (c.get("type") or "Note").title())
            code = f"  [{c['code']}]" if c.get("code") else ""
            doc.add_paragraph(f"• [{lbl}] {c.get('text', '')}{code}")

    _section("Client-facing draft — share only at your discretion "
             "(the client cannot see this unless you give it to them)",
             summary.get("client_recap"))

    doc.add_paragraph("─" * 40)
    doc.add_heading("Transcript", level=2)
