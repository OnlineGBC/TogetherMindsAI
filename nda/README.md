# Password-protected NDA (PDF + DOCX)

`build_nda.py` generates a one-way (unilateral) Non-Disclosure Agreement as both a
PDF and a DOCX, each **AES-encrypted with an open password** so recipients must
enter the password to open the file.

- **Disclosing Party:** TogetherMindsAI, ai4org, and Global Business Consulting, Inc.
- **Password:** read from the **`NDA_SECRET`** env var (the repo `.env`, or the
  environment). It is never hardcoded; the script reads it at generation time.
- Placeholders to edit per use: `[Effective Date]`, `[State/Jurisdiction]`, the term
  `[3]` years, and the Receiving Party signature block.

This is standard boilerplate, **not legal advice** — have counsel review before real use.

## Setup
```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # fpdf2, python-docx, msoffcrypto-tool
```

## Generate
1. Put the password in the repo `.env`:  `NDA_SECRET=your-password-here`
   (and, if you keep a copy in Google Secret Manager, store the same value there as your record).
2. Run:
```
.venv/Scripts/python build_nda.py
```
Outputs `NDA.pdf` and `NDA.docx` in this folder, both opening only with `NDA_SECRET`.

The generated `NDA.pdf` / `NDA.docx` are committed (they are encrypted and open
only with `NDA_SECRET`). Only `.venv/` and `__pycache__/` are gitignored.
