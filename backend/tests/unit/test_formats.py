"""TXT, Markdown, CSV and DOCX: parsers, structured chunking, validation and hostile inputs."""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import (
    DocumentValidationError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)
from app.ingestion import formats
from app.ingestion.chunking import Block, chunk_blocks, contextual_text
from app.ingestion.parsing import SandboxedPdfParser, parse_bytes
from app.ingestion.validation import validate_pdf
from tests.docs_formats import (
    docx_bytes,
    docx_with_entity_bomb,
    docx_with_macro,
    docx_with_xxe,
    docx_zip_bomb,
    not_a_docx_zip,
)
from tests.pdfs import sample_text_pdf

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "dev",
        "jwt_secret": "x" * 40,
        "mfa_encryption_key": "dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE=",
    }
    return Settings.build(**{**base, **overrides})


def write(data: bytes, name: str) -> str:
    path = Path(tempfile.mkdtemp()) / name
    path.write_bytes(data)
    return str(path)


# ================================================================ text decoding


def test_utf8_utf8_bom_and_utf16_are_decoded() -> None:
    assert (
        formats.read_text(write("héllo wörld".encode(), "a.txt"), max_bytes=1000) == "héllo wörld"
    )
    assert formats.read_text(write(b"\xef\xbb\xbfwith bom", "b.txt"), max_bytes=1000) == "with bom"
    assert (
        formats.read_text(write("utf sixteen".encode("utf-16"), "c.txt"), max_bytes=1000)
        == "utf sixteen"
    )
    assert formats.read_text(write(b"a\r\nb\rc", "d.txt"), max_bytes=1000) == "a\nb\nc"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"caf\xe9 in latin-1", "DOCUMENT_INVALID_ENCODING"),
        (b"binary\x00\x01\x02 data", "DOCUMENT_INVALID_FORMAT"),
        (b"   \n\t  \n", "DOCUMENT_EMPTY"),
        (b"x" * 2000, "DOCUMENT_TOO_LARGE"),
    ],
)
def test_bad_text_is_rejected_with_a_specific_code(data: bytes, code: str) -> None:
    with pytest.raises(formats.FormatError) as e:
        formats.read_text(write(data, "t.txt"), max_bytes=1000)
    assert e.value.code == code


# ================================================================ markdown


def kinds(blocks: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
    return [(b["kind"], b["text"], b["level"]) for b in blocks]


def test_markdown_structure_is_extracted() -> None:
    md = """# Title

Intro paragraph with **bold**, _italic_, `code` and a [link](http://example.com/x).

## Section A

- first item
- second item
1. numbered one

> quoted text

Setext Heading
==============

Sub heading
-----------

Last paragraph
spanning two lines.
"""
    result = kinds(formats.parse_markdown(md))
    assert ("heading", "Title", 1) in result
    assert ("paragraph", "Intro paragraph with bold, italic, code and a link.", 0) in result
    assert ("heading", "Section A", 2) in result
    assert [r for r in result if r[0] == "item"] == [
        ("item", "first item", 0),
        ("item", "second item", 0),
        ("item", "numbered one", 0),
    ]
    assert ("paragraph", "quoted text", 0) in result
    assert ("heading", "Setext Heading", 1) in result
    assert ("heading", "Sub heading", 2) in result
    assert ("paragraph", "Last paragraph spanning two lines.", 0) in result


def test_markdown_code_fences_and_tables_and_html() -> None:
    md = """Text before.

```python
def f():
    return "# not a heading"
```

| Name | Price |
|------|-------|
| Tea  | 2.50  |
| Cake | 4.00  |

<div class="x">Inline <b>html</b> removed</div>
"""
    blocks = formats.parse_markdown(md)
    code = next(b for b in blocks if b["kind"] == "item")
    assert 'return "# not a heading"' in code["text"]
    assert not any(b["kind"] == "heading" for b in blocks)  # '#' inside a fence is code
    rows = [b["text"] for b in blocks if b["kind"] == "row"]
    assert rows == ["Name: Tea; Price: 2.50", "Name: Cake; Price: 4.00"]
    assert any("Inline html removed" in b["text"] for b in blocks)


# ================================================================ csv


def test_csv_rows_carry_their_column_names() -> None:
    blocks = formats.parse_csv(
        "Region,Revenue,Growth\nNorth,4.2M,12%\nSouth,3.1M,\n", max_rows=100, max_cols=50
    )
    assert blocks[0]["text"] == "Columns: Region, Revenue, Growth."
    assert [b["text"] for b in blocks[1:]] == [
        "Region: North; Revenue: 4.2M; Growth: 12%",
        "Region: South; Revenue: 3.1M",  # the empty cell is skipped
    ]


@pytest.mark.parametrize("delimiter", [";", "\t", "|"])
def test_csv_delimiters_are_sniffed(delimiter: str) -> None:
    text = delimiter.join(["a", "b", "c"]) + "\n" + delimiter.join(["1x", "2y", "3z"]) + "\n"
    blocks = formats.parse_csv(text, max_rows=10, max_cols=10)
    assert blocks[1]["text"] == "a: 1x; b: 2y; c: 3z"


def test_csv_without_a_header_row_gets_generated_names_and_quoted_newlines_work() -> None:
    blocks = formats.parse_csv('1,2\n3,"line one\nline two"\n', max_rows=10, max_cols=10)
    assert blocks[1]["text"] == "column 1: 1; column 2: 2"
    assert "line one\nline two" in blocks[2]["text"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "DOCUMENT_EMPTY"),
        ("a,b\n" + "1,2\n" * 11, "DOCUMENT_TOO_MANY_ROWS"),
        (
            ",".join(f"c{i}" for i in range(60)) + "\n" + ",".join("1" for _ in range(60)),
            "DOCUMENT_TOO_MANY_COLUMNS",
        ),
    ],
)
def test_csv_limits(text: str, code: str) -> None:
    with pytest.raises(formats.FormatError) as e:
        formats.parse_csv(text, max_rows=10, max_cols=50)
    assert e.value.code == code


# ================================================================ docx


def test_docx_structure_headings_lists_and_tables() -> None:
    path = write(docx_bytes(), "a.docx")
    blocks = formats.parse_docx(path, max_rows=100)
    result = kinds(blocks)
    assert ("heading", "Quarterly Report", 1) in result
    assert ("heading", "Financial summary", 2) in result
    assert ("item", "Revenue is up", 0) in result
    rows = [b["text"] for b in blocks if b["kind"] == "row"]
    assert rows == [
        "Region: North; Revenue: 4.2M; Growth: 12%",
        "Region: South; Revenue: 3.1M; Growth: 8%",
    ]
    order = [b["text"] for b in blocks]
    assert order.index("Financial summary") < order.index("Revenue is up") < order.index(rows[0])
    assert order.index(rows[1]) < order.index("Outlook")  # document order is preserved


def zip_limits() -> dict[str, int]:
    return {"max_entries": 3000, "max_uncompressed": 200 * 1024 * 1024, "max_ratio": 100}


def test_valid_docx_passes_the_zip_checks() -> None:
    formats.check_docx_zip(write(docx_bytes(), "ok.docx"), **zip_limits())


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (docx_with_macro(), "DOCUMENT_UNSAFE"),
        (docx_zip_bomb(), "DOCUMENT_UNSAFE"),
        (not_a_docx_zip(), "DOCUMENT_INVALID_FORMAT"),
        (b"PK\x03\x04 truncated nonsense", "DOCUMENT_INVALID_FORMAT"),
    ],
)
def test_hostile_docx_packages_are_rejected(data: bytes, code: str) -> None:
    with pytest.raises(formats.FormatError) as e:
        formats.check_docx_zip(write(data, "bad.docx"), **zip_limits())
    assert e.value.code == code


def test_docx_cannot_read_local_files_through_xml_external_entities(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-FILE-CONTENT-98765")
    path = write(docx_with_xxe(secret.as_posix()), "xxe.docx")
    try:
        blocks = formats.parse_docx(path, max_rows=100)
        text = " ".join(b["text"] for b in blocks)
    except Exception:
        text = ""  # rejecting the document outright is also a safe outcome
    assert "TOP-SECRET-FILE-CONTENT-98765" not in text


# ================================================================ structured chunking


def test_blocks_become_section_aware_chunks_with_context() -> None:
    blocks = [
        Block("heading", "Report", level=1),
        Block("paragraph", "Intro sentence one is here. Intro sentence two follows it. " * 6),
        Block("heading", "Financial summary", level=2),
        Block("paragraph", "Costs decreased by four percent. Revenue grew by twelve percent. " * 6),
    ]
    chunks = chunk_blocks(blocks, target_tokens=60, max_tokens=90, min_tokens=20)
    assert len(chunks) >= 3
    assert chunks[0].section_path == "Report"
    financial = [c for c in chunks if "Costs decreased" in c.text]
    assert financial and all(c.section_path == "Report > Financial summary" for c in financial)
    assert contextual_text(financial[0], title="Q3").startswith("Q3 > Report > Financial summary\n")


def test_rows_are_never_split_and_keep_their_column_names() -> None:
    rows = [Block("row", f"Region: R{i}; Revenue: {i}.5M; Growth: {i}%") for i in range(120)]
    chunks = chunk_blocks(
        [Block("paragraph", "Columns: Region, Revenue, Growth."), *rows],
        target_tokens=80,
        max_tokens=120,
        min_tokens=20,
        min_chars=8,
    )
    assert len(chunks) > 4
    seen: list[str] = []
    for chunk in chunks:
        for line in chunk.text.splitlines():
            if line.startswith("Region:"):
                assert "Revenue:" in line and "Growth:" in line  # always a complete row
                seen.append(line)
    assert {f"Region: R{i}; Revenue: {i}.5M; Growth: {i}%" for i in range(120)} <= set(seen)


# ================================================================ validation


def check(data: bytes, name: str, mime: str | None, **kw: Any):
    return validate_pdf(io.BytesIO(data), filename=name, content_type=mime, settings=settings(**kw))


@pytest.mark.parametrize(
    ("data", "name", "mime", "fmt"),
    [
        (b"Plain text document with some words in it.", "notes.txt", "text/plain", "txt"),
        (b"# Title\n\nSome markdown text here.", "readme.md", "text/markdown", "md"),
        (b"# Title\n\nAnother markdown file.", "readme.markdown", None, "md"),
        (b"name,age\nAnn,31\nBob,27\n", "people.csv", "text/csv", "csv"),
        (docx_bytes(), "report.docx", DOCX_MIME, "docx"),
        (sample_text_pdf(), "report.PDF", "application/pdf", "pdf"),
    ],
)
def test_supported_formats_validate(data: bytes, name: str, mime: str | None, fmt: str) -> None:
    result = check(data, name, mime)
    assert result.source_format == fmt
    assert result.size == len(data)


@pytest.mark.parametrize(
    "name", ["virus.exe", "photo.png", "old.doc", "sheet.xlsx", "page.html", "noext"]
)
def test_unsupported_extensions_are_415(name: str) -> None:
    with pytest.raises(UnsupportedMediaTypeError) as e:
        check(b"data", name, None)
    assert e.value.status_code == 415
    assert "PDF, DOCX, TXT, MD, CSV" in e.value.message


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("a.txt", "image/png"),
        ("a.csv", "application/pdf"),
        ("a.docx", "text/plain"),
        ("a.md", "application/json"),
    ],
)
def test_declared_mime_must_fit_the_format(name: str, mime: str) -> None:
    with pytest.raises(UnsupportedMediaTypeError):
        check(b"hello world", name, mime)


def test_content_must_match_the_extension() -> None:
    with pytest.raises(DocumentValidationError) as e:
        check(b"this is plain text, not a zip", "fake.docx", None)
    assert e.value.code == "DOCUMENT_INVALID_FORMAT"
    with pytest.raises(DocumentValidationError) as e:
        check(b"this is not a pdf", "fake.pdf", None)
    assert e.value.code == "DOCUMENT_INVALID_FORMAT"
    with pytest.raises(DocumentValidationError) as e:
        check(not_a_docx_zip(), "zip.docx", None)
    assert e.value.code == "DOCUMENT_INVALID_FORMAT"


@pytest.mark.parametrize(
    ("data", "name", "code"),
    [
        (b"caf\xe9", "latin.txt", "DOCUMENT_INVALID_ENCODING"),
        (b"bin\x00ary", "bin.csv", "DOCUMENT_INVALID_FORMAT"),
        (b"  \n ", "blank.md", "DOCUMENT_EMPTY"),
        (docx_with_macro(), "macro.docx", "DOCUMENT_UNSAFE"),
        (docx_zip_bomb(), "bomb.docx", "DOCUMENT_UNSAFE"),
    ],
)
def test_format_specific_rejections(data: bytes, name: str, code: str) -> None:
    with pytest.raises(DocumentValidationError) as e:
        check(data, name, None, max_pdf_bytes=300 * 1024 * 1024)
    assert e.value.code == code


def test_text_formats_have_their_own_size_limit() -> None:
    with pytest.raises(PayloadTooLargeError) as e:
        check(b"x " * 600, "big.txt", "text/plain", max_text_bytes=1024)
    assert e.value.code == "DOCUMENT_TOO_LARGE"
    assert (
        check(sample_text_pdf(), "ok.pdf", "application/pdf", max_text_bytes=1024).source_format
        == "pdf"
    )


# ================================================================ end to end through the sandbox


def parse(data: bytes, fmt: str, **kw: Any):
    parser = SandboxedPdfParser(settings(sandbox_mode="oneshot", **kw))
    return parse_bytes(parser, data, title="Doc", page_count=1, source_format=fmt)


def test_markdown_is_parsed_chunked_and_cited_by_section() -> None:
    md = (
        "# Guide\n\n## Upload limits\n\n"
        + "Files above twenty five megabytes are rejected by the gateway. " * 5
    )
    result = parse(md.encode(), "md")
    assert result.parser == "structured"
    assert (result.ocr_status, result.ocr_pages) == ("not_needed", 0)
    assert any(c.section_path == "Guide > Upload limits" for c in result.chunks)
    assert all(c.embed_text.startswith("Doc") for c in result.chunks)


def test_csv_chunks_keep_whole_rows_and_context() -> None:
    lines = ["city,population,country"] + [f"City{i},{i * 1000},Land{i % 5}" for i in range(300)]
    result = parse("\n".join(lines).encode(), "csv")
    assert len(result.chunks) > 3
    text = "\n".join(c.text for c in result.chunks)
    for i in (0, 150, 299):
        assert f"city: City{i}; population: {i * 1000}; country: Land{i % 5}" in text


def test_docx_end_to_end_keeps_table_rows_under_their_heading() -> None:
    result = parse(docx_bytes(), "docx")
    assert result.parser == "structured"
    chunk = next(c for c in result.chunks if "Region: North" in c.text)
    # a short document fits one chunk; its structure stays visible inside the text, in order
    text = chunk.text
    assert text.index("Financial summary") < text.index("Region: North")
    assert "Revenue: 4.2M" in text and "Growth: 8%" in text
    outlook = next(c for c in result.chunks if "Management expects growth" in c.text)
    assert outlook.section_path == "Quarterly Report > Outlook"  # its own section chunk


def test_plain_text_goes_through_the_same_repair_and_heading_rules_as_pdf() -> None:
    text = (
        "INTRODUCTION\n\nThe organisation collected inform-\nation about performance.\n\n"
        "SUMMARY\n\nCosts fell."
    )
    result = parse(text.encode(), "txt")
    joined = " ".join(c.text for c in result.chunks)
    assert "information about performance" in joined  # hyphenation repaired
    assert result.chunks[0].section_path == "INTRODUCTION"


def test_entity_expansion_bomb_is_contained_by_the_sandbox() -> None:
    try:
        result = parse(
            docx_with_entity_bomb(), "docx", parse_memory_mb=512, parse_timeout_base_s=30
        )
        text = " ".join(c.text for c in result.chunks)
        assert len(text) < 1_000_000  # never expanded to a billion
    except Exception as exc:  # a clean rejection is fine: it must not hang or crash the test run
        assert type(exc).__name__ in {"ParseError", "SandboxError"}


def test_temp_files_are_cleaned_up() -> None:
    before = {p for p in Path(tempfile.gettempdir()).glob("doc-*")}
    parse(b"hello world, this is a text file", "txt")
    assert {p for p in Path(tempfile.gettempdir()).glob("doc-*")} <= before
    assert os.path.exists(tempfile.gettempdir())
