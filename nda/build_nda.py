"""Generate a password-protected one-way NDA as PDF and DOCX.

The open password is read from NDA_SECRET (environment, or the repo .env) — it is
never hardcoded. Recipients need that password to open either file.

Setup:   python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
Run:     NDA_SECRET must be set (in the repo .env or the environment), then:
         .venv/Scripts/python build_nda.py
Output:  ./NDA.pdf  and  ./NDA.docx   (both AES-encrypted with NDA_SECRET)
"""
import io
import os
import sys

from fpdf import FPDF
from fpdf.enums import AccessPermission
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from msoffcrypto.format.ooxml import OOXMLFile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PDF_OUT = os.path.join(HERE, "NDA.pdf")
DOCX_OUT = os.path.join(HERE, "NDA.docx")

sys.path.insert(0, REPO)
from nda_content import TITLE, SUBTITLE, INTRO, SECTIONS, SIGN_INTRO


def get_secret():
    pw = os.environ.get("NDA_SECRET")
    if pw:
        return pw
    envp = os.path.join(REPO, ".env")
    if os.path.exists(envp):
        for line in open(envp, encoding="utf-8"):
            line = line.strip()
            if line.startswith("NDA_SECRET="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def pdf_safe(s):
    """fpdf2's core Helvetica is latin-1 only; map smart punctuation to ASCII."""
    return (s.replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'")
             .replace("—", "-").replace("–", "-"))


def build_pdf(password):
    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(20, 18, 20)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, TITLE, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, SUBTITLE, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)

    pdf.set_font("Helvetica", "", 10.5)
    pdf.multi_cell(0, 5.4, pdf_safe(INTRO), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    for heading, body in SECTIONS:
        pdf.set_font("Helvetica", "B", 11)
        pdf.multi_cell(0, 6, pdf_safe(heading), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10.5)
        pdf.multi_cell(0, 5.4, pdf_safe(body), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)

    pdf.ln(3)
    pdf.set_font("Helvetica", "", 10.5)
    pdf.multi_cell(0, 5.4, pdf_safe(SIGN_INTRO), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    for label in ("Receiving Party (print name): ______________________________",
                  "Signature: ______________________________",
                  "Title / Organization: ______________________________",
                  "Date: ______________________________"):
        pdf.cell(0, 8, label, new_x="LMARGIN", new_y="NEXT")

    # Open password = NDA_SECRET (recipients must enter it to view the document).
    pdf.set_encryption(owner_password=password, user_password=password,
                       permissions=AccessPermission.all())
    pdf.output(PDF_OUT)


def build_docx(password):
    doc = Document()
    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = h.add_run(TITLE)
    r.bold = True
    r.font.size = Pt(16)
    s = doc.add_paragraph()
    s.alignment = WD_ALIGN_PARAGRAPH.CENTER
    s.add_run(SUBTITLE).font.size = Pt(10)

    doc.add_paragraph(INTRO)
    for heading, body in SECTIONS:
        hp = doc.add_paragraph()
        hp.add_run(heading).bold = True
        doc.add_paragraph(body)

    doc.add_paragraph()
    doc.add_paragraph(SIGN_INTRO)
    for label in ("Receiving Party (print name): ______________________________",
                  "Signature: ______________________________",
                  "Title / Organization: ______________________________",
                  "Date: ______________________________"):
        doc.add_paragraph(label)

    # Save to memory, then encrypt with the open password = NDA_SECRET.
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    with open(DOCX_OUT, "wb") as out:
        OOXMLFile(buf).encrypt(password, out)


def main():
    pw = get_secret()
    if not pw:
        sys.exit("NDA_SECRET is not set. Add NDA_SECRET=<password> to the repo .env "
                 "(or export it), then re-run.")
    build_pdf(pw)
    build_docx(pw)
    print(f"Wrote (encrypted with NDA_SECRET):\n  {PDF_OUT}\n  {DOCX_OUT}")


if __name__ == "__main__":
    main()
