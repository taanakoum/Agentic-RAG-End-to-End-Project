# End-to-end test with real public documents (real browser, real server, real model)

Run on 2026-11-13 with `scripts/browser_tour.py` (headless Chromium) against the stack started by
`scripts/stack.py` on a Windows laptop (CPU only), model `gemini-3.5-flash-lite` (free tier). Raw data:
`browser_tour_real_docs.json`. The documents are public (not committed): NIST SP 800-63B (US government,
public domain), "Attention Is All You Need" (arXiv 1706.03762v7) and calibre's DOCX demo document.

Kept signed in after a page reload ("keep me signed in"): True.
All three documents were Ready 252.6 s after the first upload (one worker
process, documents processed one after another).

| Document | Pages | Passages | Parser | Processing time |
|---|---|---|---|---|
| attention_is_all_you_need.pdf | 15 | 58 | docling | 82.8 s |
| calibre_demo.docx | 1 | 17 | structured | 83.7 s |
| nist_sp_800-63b.pdf | 80 | 218 | docling | 244.1 s |

| Question | Answer (shortened) | Verified citations | Time to answer |
|---|---|---|---|
| What is the minimum length of a memorized secret chosen by the subscriber? | A memorized secret chosen by the subscriber must be at least 8 characters in length [1]. | [1] nist_sp_800-63b.pdf, page 24 | 4.0 s |
| Should verifiers require users to change their passwords periodically? | No, verifiers should not require memorized secrets to be changed arbitrarily (such as periodically), unless there is evidence that the authenticator h... | [1] nist_sp_800-63b.pdf, page 25 | 5.6 s |
| How many failed authentication attempts may a verifier allow before rate limiting? | Unless otherwise specified for a given authenticator, the verifier SHALL limit consecutive failed authentication attempts on a single account to no mo... | [1] nist_sp_800-63b.pdf, page 36 | 6.5 s |
| How many layers does the encoder of the base Transformer have? | The encoder of the Transformer is composed of a stack of N = 6 identical layers [1]. | [1] attention_is_all_you_need.pdf, page 3 | 3.4 s |
| What BLEU score does the big Transformer reach on WMT 2014 English-to-German? | The big Transformer model achieves a BLEU score of 28.4 on the WMT 2014 English-to-German translation task [1]. | [1] attention_is_all_you_need.pdf, page 8 | 3.4 s |
| What does the calibre demo document demonstrate? | The calibre demo document demonstrates the ability of the calibre DOCX Input plugin to convert various typographic features in a Microsoft Word docume... | [1] calibre_demo.docx, page 1 | 2.9 s |

All six answers are correct against the documents (checked by hand: 8 characters; no periodic changes;
at most 100 failed attempts; N = 6 layers; BLEU 28.4; the DOCX demonstrates calibre's DOCX conversion).
Answers took 2.9-6.5 s.

Defects found by earlier runs of this test and fixed:
- mathematical italic letters from the PDF ("𝑁 = 6 N=6") were stored as such; Docling text is now
  NFKC-normalised (this run: "N = 6");
- the model sometimes groups sources in one marker ("[S1, S2]"); only single markers were converted, so
  raw source labels reached the user and a citation had no marker in the text. Grouped markers are now
  converted ("[1, 2]"); in this run every answer carries only numbered markers.

Limits: one run, one machine, six questions written by the author for these documents; it shows the
whole system working on real, larger files, not a measured accuracy.
