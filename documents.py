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

import io

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


def transcript_pdf_buf(session_id, messages, mode, generated_at,
                       friendly_label=None, summary=None,
                       font_regular=None, font_bold=None) -> io.BytesIO:
    """Render a session transcript as a PDF in memory.

    Data is passed in (the caller does the DB/billing lookups): `messages` are
    the ChatMessage rows, `friendly_label` the optional session name, and
    `summary` the therapist-only clinical summary dict to prepend (or None)."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.add_font("DejaVu",      fname=font_regular)
    pdf.add_font("DejaVu", "B", fname=font_bold)
    pdf.add_page()

    # Title
    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(0, 10, "TogetherMindsAI — Session Transcript",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # Metadata
    pdf.set_font("DejaVu", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, f"Session ID : {session_id}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if friendly_label:
        pdf.cell(0, 6, f"Session Name : {friendly_label}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Mode       : {mode}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Generated  : {generated_at}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # Divider
    pdf.set_draw_color(200, 200, 200)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(6)

    # Therapist-only: prepend the private clinical summary + grounded ICD codes.
    if summary is not None:
        render_summary_pdf(pdf, summary)

    # Assign a distinct RGB color to each human participant
    _PDF_PARTICIPANT_COLORS = [
        (30,  80,  160),   # blue
        (146, 39,  15),    # rust red
        (107, 33,  168),   # purple
        (13,  110, 110),   # teal
        (180, 90,  9),     # amber
        (26,  92,  46),    # dark green
    ]
    pdf_participant_color: dict = {}
    _pdf_color_idx = 0
    for msg in messages:
        if msg.user_id != "AI" and msg.user_id not in pdf_participant_color:
            pdf_participant_color[msg.user_id] = _PDF_PARTICIPANT_COLORS[_pdf_color_idx % len(_PDF_PARTICIPANT_COLORS)]
            _pdf_color_idx += 1

    if not messages:
        pdf.set_text_color(120, 120, 120)
        pdf.set_font("DejaVu", "", 11)
        pdf.cell(0, 8, "No messages recorded for this session.",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        for msg in messages:
            is_ai = msg.user_id == "AI"
            if is_ai:
                speaker = "AI Co-Pilot"
            elif msg.display_name:
                speaker = f"{session_id}-{msg.display_name}"
            else:
                speaker = "User"
            ts = msg.timestamp.strftime("%Y-%m-%d %H:%M")

            # Speaker + timestamp line
            pdf.set_font("DejaVu", "B", 10)
            if is_ai:
                pdf.set_text_color(30, 120, 60)
            else:
                r, g, b = pdf_participant_color.get(msg.user_id, (30, 80, 160))
                pdf.set_text_color(r, g, b)
            pdf.cell(0, 7, f"{speaker}  [{ts}]",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            # Message body
            pdf.set_font("DejaVu", "", 11)
            pdf.set_text_color(30, 30, 30)
            pdf.multi_cell(0, 6, msg.text)
            pdf.ln(3)

    buf = io.BytesIO(pdf.output())
    buf.seek(0)
    return buf


def transcript_docx_buf(session_id, messages, mode, generated_at,
                        friendly_label=None, summary=None) -> io.BytesIO:
    """Render a session transcript as a DOCX in memory. See transcript_pdf_buf
    for the data-in contract."""
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()

    # Title
    title = doc.add_heading("TogetherMindsAI — Session Transcript", level=1)
    title.runs[0].font.color.rgb = RGBColor(0x1E, 0x78, 0x40)

    # Metadata
    meta = doc.add_paragraph()
    meta.add_run("Session ID : ").bold = True
    meta.add_run(session_id)
    if friendly_label:
        meta_name = doc.add_paragraph()
        meta_name.add_run("Session Name : ").bold = True
        meta_name.add_run(friendly_label)
    meta2 = doc.add_paragraph()
    meta2.add_run("Mode       : ").bold = True
    meta2.add_run(mode)
    meta3 = doc.add_paragraph()
    meta3.add_run("Generated  : ").bold = True
    meta3.add_run(generated_at)

    doc.add_paragraph("─" * 40)

    # Therapist-only: prepend the private clinical summary + grounded ICD codes.
    if summary is not None:
        render_summary_docx(doc, summary)

    # Assign a distinct color to each human participant (AI is always green)
    _PARTICIPANT_COLORS = [
        RGBColor(0x1E, 0x50, 0xA0),  # blue
        RGBColor(0x92, 0x27, 0x0F),  # rust red
        RGBColor(0x6B, 0x21, 0xA8),  # purple
        RGBColor(0x0D, 0x6E, 0x6E),  # teal
        RGBColor(0xB4, 0x5A, 0x09),  # amber
        RGBColor(0x1A, 0x5C, 0x2E),  # dark green
    ]
    participant_color: dict = {}
    _color_idx = 0
    for msg in messages:
        if msg.user_id != "AI" and msg.user_id not in participant_color:
            participant_color[msg.user_id] = _PARTICIPANT_COLORS[_color_idx % len(_PARTICIPANT_COLORS)]
            _color_idx += 1

    if not messages:
        p = doc.add_paragraph("No messages recorded for this session.")
        p.runs[0].italic = True
    else:
        for msg in messages:
            is_ai = msg.user_id == "AI"
            if is_ai:
                speaker = "AI Co-Pilot"
            elif msg.display_name:
                speaker = f"{session_id}-{msg.display_name}"
            else:
                speaker = "User"
            ts = msg.timestamp.strftime("%Y-%m-%d %H:%M")

            # Speaker heading
            p = doc.add_paragraph()
            run = p.add_run(f"{speaker}  [{ts}]")
            run.bold = True
            run.font.size = Pt(10)
            if is_ai:
                run.font.color.rgb = RGBColor(0x1E, 0x78, 0x3C)
            else:
                run.font.color.rgb = participant_color.get(msg.user_id, RGBColor(0x1E, 0x50, 0xA0))

            # Message body
            doc.add_paragraph(msg.text)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf
