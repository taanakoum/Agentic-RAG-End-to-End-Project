"""Entry points executed INSIDE the sandboxed child process.

    python -m app.ingestion.sandbox_jobs <inspect|light|docling> <pdf_path> '<options json>'

Prints exactly one JSON object on the real stdout. All library noise (Docling, RapidOCR, torch
progress bars) goes to stderr. Keep imports lazy and minimal: startup time matters because the
API runs `inspect` for every upload.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
from typing import Any

from app.ingestion.textnorm import meaningful_chars, normalize_text

_CONVERTERS: dict[tuple[bool, bool], Any] = {}  # warm-process caches (see serve())
_CHUNKERS: dict[tuple[str, int], Any] = {}
OCR_CACHE: dict[str, Any] = {}


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message}


def _open_reader(path: str) -> Any:
    from pypdf import PdfReader

    return PdfReader(path, strict=False)


def _non_pdf(command: str, path: str, opts: dict[str, Any]) -> dict[str, Any]:
    """Inspect / parse TXT, Markdown, CSV, DOCX. Rejects return {"ok": False, code, message}."""
    from app.ingestion import formats

    fmt = str(opts["format"])
    try:
        if fmt == "docx":
            formats.check_docx_zip(
                path,
                max_entries=int(opts["docx_max_entries"]),
                max_uncompressed=int(opts["docx_max_uncompressed"]),
                max_ratio=int(opts["docx_max_ratio"]),
            )
            blocks = formats.parse_docx(path, max_rows=int(opts["max_table_rows"]))
        else:
            text = formats.read_text(path, max_bytes=int(opts["max_text_bytes"]))
            if fmt == "txt":
                if command == "inspect":
                    return {"ok": True, "encrypted": False, "page_count": 1}
                return {
                    "ok": True,
                    "page_count": 1,
                    "ocr_status": "not_needed",
                    "ocr_pages": 0,
                    "pages": [{"page_number": 1, "text": text, "from_ocr": False}],
                }
            if fmt == "md":
                blocks = formats.parse_markdown(text)
            elif fmt == "csv":
                blocks = formats.parse_csv(
                    text, max_rows=int(opts["max_table_rows"]), max_cols=int(opts["max_csv_cols"])
                )
            else:
                return _fail("DOCUMENT_UNSUPPORTED_TYPE", f"Unsupported format: {fmt}")
    except formats.FormatError as exc:
        return _fail(exc.code, exc.message)
    except Exception as exc:
        # python-docx / lxml / zipfile raise many unrelated exception types on hostile input.
        return _fail("DOCUMENT_CORRUPTED", f"{type(exc).__name__}: {exc}"[:300])
    if command == "inspect":
        return {"ok": True, "encrypted": False, "page_count": 1}
    return {
        "ok": True,
        "page_count": 1,
        "ocr_status": "not_needed",
        "ocr_pages": 0,
        "blocks": blocks,
    }


def cmd_inspect(path: str, opts: dict[str, Any]) -> dict[str, Any]:
    if opts.get("format", "pdf") != "pdf":
        return _non_pdf("inspect", path, opts)
    try:
        reader = _open_reader(path)
        if reader.is_encrypted:
            return {"ok": True, "encrypted": True, "page_count": 0}
        return {"ok": True, "encrypted": False, "page_count": len(reader.pages)}
    except Exception as exc:
        # pypdf raises many unrelated exception types on damaged or hostile input.
        return _fail("DOCUMENT_CORRUPTED", f"{type(exc).__name__}: {exc}"[:300])


def _text_layer(path: str) -> list[str]:
    reader = _open_reader(path)
    pages: list[str] = []
    for index in range(len(reader.pages)):
        try:
            pages.append(normalize_text(reader.pages[index].extract_text() or ""))
        except Exception:
            pages.append("")  # one unreadable page must not fail the document
    return pages


def cmd_light(path: str, opts: dict[str, Any]) -> dict[str, Any]:
    """pypdf text layer, with RapidOCR only for pages that have (almost) no text."""
    try:
        texts = _text_layer(path)
    except Exception as exc:
        return _fail("DOCUMENT_CORRUPTED", f"{type(exc).__name__}: {exc}"[:300])
    minimum = int(opts["min_text_chars_per_page"])
    needs = [i for i, t in enumerate(texts) if meaningful_chars(t) < minimum]
    status, recognised = "not_needed", 0
    from_ocr: set[int] = set()
    if needs and not opts["ocr_enabled"]:
        status = "disabled"
    elif needs:
        from app.ingestion.ocr import RapidOcrEngine

        engine = OCR_CACHE.setdefault("rapid", RapidOcrEngine())
        data = open(path, "rb").read()  # noqa: SIM115
        limit = int(opts["ocr_max_pages"])
        failures = 0
        for index in needs[:limit]:
            try:
                text = normalize_text(engine.ocr_pdf_page(data, index, dpi=int(opts["ocr_dpi"])))
            except Exception:
                failures += 1
                continue
            if meaningful_chars(text) >= minimum:
                texts[index] = text
                from_ocr.add(index)
                recognised += 1
        if recognised == 0 and failures == len(needs[:limit]):
            status = "failed"
        else:
            status = "limited" if len(needs) > limit else "used"
    return {
        "ok": True,
        "page_count": len(texts),
        "ocr_status": status,
        "ocr_pages": recognised,
        "pages": [
            {"page_number": i + 1, "text": t, "from_ocr": i in from_ocr}
            for i, t in enumerate(texts)
        ],
    }


def cmd_docling(path: str, opts: dict[str, Any]) -> dict[str, Any]:
    """Docling layout analysis + HybridChunker. OCR only if a page lacks a text layer."""
    try:
        texts = _text_layer(path)
    except Exception as exc:
        return _fail("DOCUMENT_CORRUPTED", f"{type(exc).__name__}: {exc}"[:300])
    minimum = int(opts["min_text_chars_per_page"])
    needs = [i + 1 for i, t in enumerate(texts) if meaningful_chars(t) < minimum]
    do_ocr = bool(needs) and bool(opts["ocr_enabled"])
    if needs and not opts["ocr_enabled"]:
        ocr_status = "disabled"
    elif needs and len(needs) > int(opts["ocr_max_pages"]):
        return _fail(
            "DOCUMENT_OCR_LIMIT",
            f"{len(needs)} pages need OCR; the limit is {opts['ocr_max_pages']}.",
        )
    else:
        ocr_status = "used" if needs else "not_needed"

    # Docling and transformers are only partially typed; keep them behind `Any`.
    chunking: Any = importlib.import_module("docling.chunking")
    base_models: Any = importlib.import_module("docling.datamodel.base_models")
    pipeline_options: Any = importlib.import_module("docling.datamodel.pipeline_options")
    converter_module: Any = importlib.import_module("docling.document_converter")
    hf_tokenizer: Any = importlib.import_module(
        "docling_core.transforms.chunker.tokenizer.huggingface"
    )
    transformers: Any = importlib.import_module("transformers")
    HybridChunker, DocumentStream, InputFormat = (
        chunking.HybridChunker,
        base_models.DocumentStream,
        base_models.InputFormat,
    )
    PdfPipelineOptions, RapidOcrOptions = (
        pipeline_options.PdfPipelineOptions,
        pipeline_options.RapidOcrOptions,
    )
    DocumentConverter, PdfFormatOption = (
        converter_module.DocumentConverter,
        converter_module.PdfFormatOption,
    )
    HuggingFaceTokenizer = hf_tokenizer.HuggingFaceTokenizer
    AutoTokenizer = transformers.AutoTokenizer

    pipeline = PdfPipelineOptions(do_ocr=do_ocr, do_table_structure=bool(opts["docling_tables"]))
    if do_ocr:
        pipeline.ocr_options = RapidOcrOptions()
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)}
    )
    with open(path, "rb") as handle:
        stream = DocumentStream(name="document.pdf", stream=io.BytesIO(handle.read()))
    document = converter.convert(stream).document
    chunker_key = (str(opts["tokenizer"]), int(opts["max_tokens"]))
    chunker = _CHUNKERS.get(chunker_key)
    if chunker is None:
        chunker = HybridChunker(
            tokenizer=HuggingFaceTokenizer(
                tokenizer=AutoTokenizer.from_pretrained(chunker_key[0]),
                max_tokens=chunker_key[1],
            )
        )
        _CHUNKERS[chunker_key] = chunker
    chunks: list[dict[str, Any]] = []
    chunk: Any
    for chunk in chunker.chunk(document):
        pages = sorted({p.page_no for item in chunk.meta.doc_items for p in item.prov if p.page_no})
        headings = list(chunk.meta.headings or [])
        chunks.append(
            {
                "text": chunk.text,
                "contextual": chunker.contextualize(chunk),
                "pages": pages,
                "headings": headings,
            }
        )
    # HybridChunker only emits chunks for body content; headings are context. Text it recognised
    # but did not chunk (typically scanned text that the layout model labelled a heading) would
    # be lost silently, so every page with recognised text and no chunk coverage is returned as
    # raw per-page text for the parent to chunk.
    covered = {p for c in chunks for p in c["pages"]}
    per_page: dict[int, list[str]] = {}
    for item in document.texts:
        page_no = int(item.prov[0].page_no) if item.prov else 1
        if page_no not in covered and str(item.text).strip():
            per_page.setdefault(page_no, []).append(str(item.text))
    fallback_pages: list[dict[str, Any]] = [
        {"page_number": n, "text": "\n\n".join(parts), "from_ocr": do_ocr and n in needs}
        for n, parts in sorted(per_page.items())
    ]
    pages_with_text = covered | set(per_page)
    recovered = sum(1 for p in needs if p in pages_with_text)
    if needs and do_ocr and recovered == 0:
        ocr_status = "failed"
    return {
        "ok": True,
        "page_count": len(texts),
        "ocr_status": ocr_status,
        "ocr_pages": recovered if do_ocr else 0,
        "chunks": chunks,
        "fallback_pages": fallback_pages,
    }


def cmd_structured(path: str, opts: dict[str, Any]) -> dict[str, Any]:
    return _non_pdf("parse", path, opts)


COMMANDS = {
    "inspect": cmd_inspect,
    "light": cmd_light,
    "docling": cmd_docling,
    "structured": cmd_structured,
}


def serve() -> None:
    """Warm mode: answer JSON-line requests on stdin until stdin closes.

    Heavy objects (Docling converters, tokenizer, OCR engine) are cached in this process, so only
    the first request pays for loading models. An exception while handling one request is
    reported as `crashed` (the parent treats it like a crash and may recycle this process).
    """
    real_stdout = sys.stdout
    sys.stdout = sys.stderr  # library chatter must never corrupt the JSON channel
    real_stdout.write(json.dumps({"ready": True}) + "\n")
    real_stdout.flush()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            result = COMMANDS[request["command"]](request["path"], request.get("opts", {}))
        except BaseException as exc:
            result = {"ok": False, "crashed": True, "message": f"{type(exc).__name__}: {exc}"[:400]}
        real_stdout.write(json.dumps(result) + "\n")
        real_stdout.flush()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
        return
    real_stdout = sys.stdout
    sys.stdout = sys.stderr  # library chatter must never corrupt the JSON channel
    command, path, raw_opts = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "{}"
    result = COMMANDS[command](path, json.loads(raw_opts))
    real_stdout.write(json.dumps(result))
    real_stdout.flush()


if __name__ == "__main__":
    main()
