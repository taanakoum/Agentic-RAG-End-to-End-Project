"""Builders for non-PDF test documents (DOCX, plus hostile variants)."""

from __future__ import annotations

import io
import zipfile

from docx import Document


def docx_bytes() -> bytes:
    """A Word document with headings, paragraphs, a list and a table."""
    doc = Document()
    doc.add_heading("Quarterly Report", level=1)
    doc.add_paragraph(
        "This report summarises the quarterly results of the organisation. "
        "Revenue grew steadily across all regions during the period under review."
    )
    doc.add_heading("Financial summary", level=2)
    doc.add_paragraph(
        "Operating costs decreased while net income increased compared with last year."
    )
    doc.add_paragraph("Revenue is up", style="List Bullet")
    doc.add_paragraph("Costs are down", style="List Bullet")
    table = doc.add_table(rows=3, cols=3)
    for col, text in enumerate(["Region", "Revenue", "Growth"]):
        table.rows[0].cells[col].text = text
    for col, text in enumerate(["North", "4.2M", "12%"]):
        table.rows[1].cells[col].text = text
    for col, text in enumerate(["South", "3.1M", "8%"]):
        table.rows[2].cells[col].text = text
    doc.add_heading("Outlook", level=2)
    doc.add_paragraph("Management expects growth of between 8 and 10 percent next year.")
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _rewrite(
    original: bytes, replacements: dict[str, bytes], extra: dict[str, bytes] | None = None
) -> bytes:
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(original)) as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for info in src.infolist():
            dst.writestr(info.filename, replacements.get(info.filename, src.read(info.filename)))
        for name, data in (extra or {}).items():
            dst.writestr(name, data)
    return out.getvalue()


def docx_with_macro() -> bytes:
    return _rewrite(docx_bytes(), {}, {"word/vbaProject.bin": b"MZ fake macro payload"})


def docx_zip_bomb() -> bytes:
    """Highly compressible 120 MB part: tiny on disk, huge when expanded."""
    return _rewrite(docx_bytes(), {}, {"word/media/bomb.bin": b"\x00" * (120 * 1024 * 1024)})


def docx_with_xxe(target_file: str) -> bytes:
    """document.xml that tries to inline a local file through an external XML entity."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<!DOCTYPE d [<!ENTITY leak SYSTEM "file:///{target_file}">]>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Visible text before. &leak; After.</w:t></w:r></w:p></w:body>"
        "</w:document>"
    ).encode()
    return _rewrite(docx_bytes(), {"word/document.xml": xml})


def docx_with_entity_bomb() -> bytes:
    """'Billion laughs': nested entity expansion in document.xml."""
    levels = ['<!ENTITY a0 "lol">']
    for i in range(1, 10):
        levels.append(
            f'<!ENTITY a{i} "{"&a" + str(i - 1) + ";" * 1}' * 0
            + "".join(f"&a{i - 1};" for _ in range(10))
            + '">'
        )
    xml = (
        '<?xml version="1.0"?>\n<!DOCTYPE d [' + "".join(levels) + "]>\n"
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&a9;</w:t></w:r></w:p></w:body></w:document>"
    ).encode()
    return _rewrite(docx_bytes(), {"word/document.xml": xml})


def not_a_docx_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("hello.txt", "just a zip")
    return out.getvalue()
