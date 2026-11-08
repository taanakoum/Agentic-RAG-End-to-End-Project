# Chunking evaluation (synthetic corpus, real embedding models)

Corpus: 6 documents, 43 pages, 36 questions, 3 of them whose answer sentence continues across a page break (synthetic, authored for this project; see corpus.py).
Docling PDF conversion (layout analysis, CPU) took 72.7s in total = 1.69 s/page; it is included in the s/page of E and F only through this line (their column shows chunking after conversion).

| Strategy | chunks | mean tok | fact kept | dense hit@1 | dense MRR | hybrid hit@1 | hybrid hit@3 | hybrid hit@5 | hybrid MRR | s/page |
|---|---|---|---|---|---|---|---|---|---|---|
| A baseline (fixed 1000 chars, per page, raw) | 150 | 149 | 0.92 | 0.47 | 0.59 | 0.47 | 0.56 | 0.61 | 0.55 | 0.02 |
| B langchain recursive (clean text, 256 tok) | 112 | 181 | 0.92 | 0.50 | 0.54 | 0.42 | 0.50 | 0.53 | 0.49 | 0.01 |
| C custom, no heading context | 93 | 237 | 1.00 | 0.42 | 0.52 | 0.42 | 0.56 | 0.67 | 0.53 | 0.01 |
| D custom + heading/title context | 93 | 237 | 1.00 | 0.44 | 0.57 | 0.47 | 0.61 | 0.67 | 0.58 | 0.01 |
| E docling HybridChunker (320 tok) | 72 | 277 | 1.00 | 0.56 | 0.67 | 0.58 | 0.81 | 0.83 | 0.70 | 1.95 |
| F docling markdown + langchain headers | 114 | 177 | 1.00 | 0.64 | 0.77 | 0.58 | 0.86 | 0.89 | 0.72 | 0.00 |
