"""Single source of truth for the NDA text.

Used by the web page (templates/nda.html via the /nda route) and the file
generator (nda/build_nda.py) so the page, the PDF, and the DOCX stay identical.
"""

DISCLOSER = "TogetherMindsAI, ai4org, and Global Business Consulting, Inc."
TITLE = "NON-DISCLOSURE AGREEMENT"
SUBTITLE = "(One-Way / Unilateral)"

INTRO = (
    f"This Non-Disclosure Agreement (“Agreement”) is entered into as of "
    f"[Effective Date] by and between {DISCLOSER} (collectively, the “Disclosing "
    f"Party”), and the undersigned recipient (“Receiving Party”). The "
    f"Disclosing Party and the Receiving Party are each a “Party” and together "
    f"the “Parties.”"
)

SECTIONS = [
    ("1. Purpose",
     "The Parties wish to explore a potential business relationship (the “Purpose”). "
     "In connection with the Purpose, the Disclosing Party may disclose to the Receiving Party "
     "certain confidential and proprietary information."),
    ("2. Confidential Information",
     "“Confidential Information” means any non-public information disclosed by the "
     "Disclosing Party to the Receiving Party, whether orally, in writing, or in any other form, "
     "that is designated as confidential or that reasonably should be understood to be confidential "
     "given its nature and the circumstances of disclosure. It includes, without limitation, business "
     "plans, financials, pricing, technology, software, source code, models, prompts, algorithms, "
     "product and roadmap information, clinical and operational data, customer and patient information, "
     "trade secrets, and know-how."),
    ("3. Obligations of the Receiving Party",
     "The Receiving Party shall: (a) hold the Confidential Information in strict confidence; "
     "(b) not disclose it to any third party without the Disclosing Party’s prior written consent; "
     "(c) use it solely for the Purpose; (d) protect it using at least the same degree of care it uses "
     "for its own confidential information, and in no event less than reasonable care; and (e) limit "
     "access to its personnel and advisors who need to know it for the Purpose and who are bound by "
     "confidentiality obligations at least as protective as those herein."),
    ("4. Exclusions",
     "Confidential Information does not include information that: (a) is or becomes publicly available "
     "through no fault of the Receiving Party; (b) was rightfully known to the Receiving Party without "
     "restriction before disclosure; (c) is rightfully received from a third party without a duty of "
     "confidentiality; or (d) is independently developed by the Receiving Party without use of or "
     "reference to the Confidential Information. If disclosure is required by law or court order, the "
     "Receiving Party may disclose only as required, after giving the Disclosing Party prompt notice "
     "(where legally permitted) and reasonable cooperation to seek protective treatment."),
    ("5. Term and Survival",
     "This Agreement applies to Confidential Information disclosed during its term, which begins on the "
     "Effective Date and continues until terminated by either Party on written notice. The Receiving "
     "Party’s confidentiality obligations survive for [3] years after disclosure, except that "
     "obligations with respect to trade secrets continue for as long as the information remains a trade "
     "secret under applicable law."),
    ("6. Return or Destruction",
     "Upon the Disclosing Party’s written request, the Receiving Party shall promptly return or "
     "destroy all Confidential Information and any copies, and, if requested, certify such destruction "
     "in writing."),
    ("7. No License or Rights",
     "No license or other right to any intellectual property is granted under this Agreement. All "
     "Confidential Information remains the property of the Disclosing Party."),
    ("8. No Warranty",
     "All Confidential Information is provided “as is.” The Disclosing Party makes no "
     "warranties, express or implied, regarding its accuracy or completeness."),
    ("9. No Obligation",
     "Nothing in this Agreement obligates either Party to proceed with any transaction or relationship."),
    ("10. Remedies",
     "The Receiving Party agrees that any breach may cause irreparable harm for which monetary damages "
     "would be inadequate, and that the Disclosing Party is entitled to seek injunctive relief in "
     "addition to any other remedies available at law or in equity."),
    ("11. Governing Law",
     "This Agreement is governed by the laws of [State/Jurisdiction], without regard to its conflict-of-"
     "laws principles."),
    ("12. Miscellaneous",
     "This Agreement is the entire agreement between the Parties regarding its subject matter and "
     "supersedes all prior understandings. It may be amended only in a writing signed by both Parties. "
     "If any provision is held unenforceable, the remaining provisions remain in effect. The Receiving "
     "Party may not assign this Agreement without the Disclosing Party’s prior written consent."),
]

SIGN_INTRO = "IN WITNESS WHEREOF, the Receiving Party has executed this Agreement as of the date below."
