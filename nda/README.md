# NDA

`NDA.docx` is the **single source** for the Non-Disclosure Agreement.

- Edit `NDA.docx` in Word like any normal document.
- The web page **`/nda`** reads `NDA.docx` and shows its text — the centered
  title, the bold section headings, and the body paragraphs. The page is gated by
  a password (`NDA_SECRET`). Images and Word-specific layout are **not** shown on
  the web; the text is.
- **To update the live page:** edit `NDA.docx`, commit, and deploy. The page
  re-reads the file automatically (cached by its modified time).
- **To hand someone a copy:** just send `NDA.docx` — it's a normal, unencrypted
  Word file.

The password `NDA_SECRET` lives in `.env` locally and Google Secret Manager on
Cloud Run; it only controls who can view `/nda` online (the file itself opens
without a password).
