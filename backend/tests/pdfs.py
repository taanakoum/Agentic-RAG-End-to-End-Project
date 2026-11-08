"""Builders for test PDFs: text, scanned (image only), encrypted, corrupted, oversized."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one text page per entry (Helvetica, Latin-1 text)."""
    objects: list[bytes] = []

    def add(body: str | bytes) -> int:
        objects.append(body.encode("latin-1") if isinstance(body, str) else body)
        return len(objects)  # 1-based object number

    catalog = add("")  # placeholder, filled below
    pages_obj = add("")
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    for text in pages:
        lines = text.split("\n")
        stream = (
            "BT /F1 11 Tf 14 TL 56 780 Td\n"
            + "\n".join(f"({_escape(line)}) Tj T*" for line in lines)
            + "\nET"
        )
        content = add(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
        page = add(
            f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 612 842] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"
        )
        page_ids.append(page)
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects[pages_obj - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return out.getvalue()


def sample_text_pdf(tag: str = "alpha") -> bytes:
    """Two pages with a heading, distinct per `tag` (so different tags hash differently)."""
    return make_pdf(
        [
            f"INTRODUCTION\nThis report ({tag}) describes the quarterly results of the company.\n"
            "Revenue grew steadily across all regions during the period under review.\n"
            "The board approved the new budget for research and development.",
            "FINANCIAL SUMMARY\nPage two covers the financial summary in detail.\n"
            "Operating costs decreased while net income increased compared with last year.\n"
            f"Reference code {tag} appears only in this document.",
        ]
    )


def scanned_pdf(lines: list[str] | None = None) -> bytes:
    """A PDF whose only content is an IMAGE of text (no text layer): needs OCR."""
    lines = lines or [
        "INVOICE NUMBER 48213",
        "TOTAL AMOUNT DUE 1250 DOLLARS",
        "PAYMENT TERMS THIRTY DAYS",
    ]
    image = Image.new("RGB", (1240, 700), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=56)
    y = 60
    for line in lines:
        draw.text((60, y), line, fill="black", font=font)
        y += 130
    buffer = io.BytesIO()
    image.save(buffer, "PDF", resolution=150.0)
    return buffer.getvalue()


def encrypted_pdf(owner_key: str = "secret") -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(owner_key)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def corrupted_pdf() -> bytes:
    """Valid header, then garbage: passes the magic-byte check, fails the parser."""
    return b"%PDF-1.7\n" + bytes(range(256)) * 20 + b"\nnot a real pdf body\n"


def truncated_pdf() -> bytes:
    return sample_text_pdf("trunc")[:400]


def blank_pdf() -> bytes:
    """A valid PDF with a page but no text and no images (nothing to extract, nothing to OCR)."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def mixed_pdf() -> bytes:
    """Two real text pages followed by one scanned (image-only) page."""
    from pypdf import PdfReader

    writer = PdfWriter()
    for source in (sample_text_pdf("mixed"), scanned_pdf()):
        for page in PdfReader(io.BytesIO(source)).pages:
            writer.add_page(page)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def sectioned_pdf(tag: str = "sect") -> bytes:
    """Two pages, each a full-size section with its own heading (enough text for 2+ chunks)."""

    def body(topic: str, n: int) -> str:
        return "\n".join(
            f"This sentence number {i} discusses {topic} and adds several more words to the page."
            for i in range(n)
        )

    return make_pdf(
        [
            f"INTRODUCTION\n{body('the introduction', 9)}",
            f"FINANCIAL SUMMARY\n{body('financial results', 9)}\n"
            f"Reference code {tag} ends the page.",
        ]
    )
