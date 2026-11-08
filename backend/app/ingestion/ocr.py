"""Open-source OCR for scanned PDF pages: pypdfium2 (render) + RapidOCR (PP-OCR models, ONNX).

The OCR models ship inside the `rapidocr` wheel, so no network access or system install
(Tesseract) is needed. The engine is created lazily once per worker process.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np
import pypdfium2 as pdfium  # pyright: ignore[reportMissingTypeStubs]


class RapidOcrEngine:
    def __init__(self) -> None:
        self._engine: Any = None
        self._lock = threading.Lock()

    def ocr_pdf_page(self, pdf_bytes: bytes, page_index: int, *, dpi: int) -> str:
        # pypdfium2 ships no type information; keep the untyped objects behind `Any`.
        document: Any = pdfium.PdfDocument(pdf_bytes)
        try:
            page: Any = document[page_index]
            image: Any = page.render(scale=dpi / 72).to_pil().convert("RGB")
        finally:
            document.close()
        with self._lock:
            engine = self._instance()
            output = engine(np.asarray(image))
        texts = getattr(output, "txts", None)
        if not texts:
            return ""
        return "\n".join(str(t) for t in texts)

    def _instance(self) -> Any:
        if self._engine is None:
            from rapidocr import RapidOCR

            self._engine = RapidOCR()
        return self._engine
