"""Synthetic, deterministic chunking-evaluation corpus (authored for this project).

HONEST LABEL: this is a small SYNTHETIC benchmark written to expose specific chunking failure
modes. It is not a general retrieval benchmark and says nothing about quality on arbitrary real
documents. Documents contain: bold sentence-case headings (a text-only heuristic cannot see
boldness; layout-aware parsers can), running headers/footers with page numbers, hyphenated line
breaks, sentences that continue across page breaks, and distractor sections that repeat a fact
template with a different value under a different heading.

Each fact is (section_heading, fact_sentence, question). The fact sentence deliberately omits the
words that identify its topic; those words appear only in the section heading, so a chunk is
only retrievable for the question if its heading context travels with it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

LINE_CHARS = 86
LINES_PER_PAGE = 41

FILLER_STARTS = [
    "The team reviewed the process and documented the findings in detail",
    "Several internal stakeholders contributed feedback during the reporting period",
    "Operations continued without major interruption throughout the quarter",
    "A working group met regularly to coordinate priorities and track progress",
    "Documentation was updated to reflect the latest agreed practices",
    "Management considered a range of alternatives before reaching a decision",
    "Previous assessments highlighted opportunities for incremental improvement",
    "Training sessions were offered to help staff adopt the revised approach",
]
FILLER_ENDS = [
    "and the outcome was shared with the relevant departments.",
    "although some details remain subject to further review.",
    "which allowed the organisation to plan the next steps with confidence.",
    "while maintaining consistency with established guidelines.",
    "and no significant concerns were raised by participants.",
    "as recorded in the minutes of the corresponding meetings.",
]


@dataclass(frozen=True)
class Fact:
    doc: str
    heading: str
    sentence: str  # the answer-bearing sentence (also the relevance key)
    question: str


@dataclass(frozen=True)
class SourceDoc:
    name: str
    title: str
    sections: list[tuple[str, list[str]]]  # (heading, paragraphs)
    facts: list[Fact]


def _doc(name: str, title: str, spec: list[tuple[str, str, str, str]], seed: int) -> SourceDoc:
    """spec rows: (heading, fact sentence, question, distractor_sentence_for_next_section)."""
    rng = random.Random(seed)

    def filler(n: int) -> str:
        return " ".join(f"{rng.choice(FILLER_STARTS)} {rng.choice(FILLER_ENDS)}" for _ in range(n))

    sections: list[tuple[str, list[str]]] = []
    facts: list[Fact] = []
    for heading, fact, question, _ in spec:
        # long sections: the fact sits deep inside, usually far from the heading
        paragraphs = [filler(5), filler(4), f"{filler(2)} {fact} {filler(3)}", filler(5), filler(4)]
        sections.append((heading, paragraphs))
        facts.append(Fact(name, heading, fact, question))
    return SourceDoc(name, title, sections, facts)


DOCS: list[SourceDoc] = [
    _doc(
        "platform_guide",
        "Platform Operations Guide",
        [
            (
                "Upload size limits",
                "Files larger than 25 megabytes are rejected by the gateway.",
                "What is the maximum upload size?",
                "",
            ),
            (
                "Request rate limits",
                "Clients exceeding 100 calls per minute receive a throttling response.",
                "How many requests per minute are allowed?",
                "",
            ),
            (
                "Session timeout",
                "Idle sessions are closed automatically after 30 minutes.",
                "When does an idle session expire?",
                "",
            ),
            (
                "Backup schedule",
                "Snapshots are taken every day at 02:00 and kept for 14 days.",
                "How often are backups made and how long are they retained?",
                "",
            ),
            (
                "Support hours",
                "The help desk answers tickets between 08:00 and 18:00 on weekdays.",
                "When is customer support available?",
                "",
            ),
            (
                "Password policy",
                "Passphrases must contain at least 14 characters and are rotated yearly.",
                "What are the requirements for passwords?",
                "",
            ),
        ],
        1,
    ),
    _doc(
        "hr_policy",
        "Employee Policy Handbook",
        [
            (
                "Annual leave",
                "Full-time staff accrue 25 days of paid vacation each calendar year.",
                "How many vacation days do employees get?",
                "",
            ),
            (
                "Remote work",
                "Working from home is permitted on up to three days per week.",
                "How many days per week can I work from home?",
                "",
            ),
            (
                "Probation period",
                "New hires complete a probation of six months before confirmation.",
                "How long is the trial period for new employees?",
                "",
            ),
            (
                "Travel expenses",
                "Receipts are required for any claim above 40 euros.",
                "Above what amount do I need receipts for reimbursement?",
                "",
            ),
            (
                "Parental leave",
                "Parents may take up to 16 weeks of leave at full salary.",
                "How long can parents stay away from work after a birth?",
                "",
            ),
            (
                "Equipment returns",
                "Laptops must be handed back within five working days of departure.",
                "When must company laptops be returned when leaving?",
                "",
            ),
        ],
        2,
    ),
    _doc(
        "annual_report",
        "Annual Report 2025",
        [
            (
                "Revenue overview",
                "Total sales reached 48.2 million euros, an increase of 12 percent.",
                "How much revenue did the company make?",
                "",
            ),
            (
                "Operating costs",
                "Expenses fell by 4 percent thanks to the consolidation of data centres.",
                "How did costs change compared with last year?",
                "",
            ),
            (
                "Workforce",
                "The company employed 1,240 people at the end of December.",
                "How many staff does the company have?",
                "",
            ),
            (
                "Customer satisfaction",
                "The survey produced a score of 4.6 out of 5 across all regions.",
                "What rating did customers give?",
                "",
            ),
            (
                "Capital expenditure",
                "Investment in new facilities totalled 6.5 million euros.",
                "How much was spent on new buildings and equipment?",
                "",
            ),
            (
                "Outlook",
                "Management expects growth of between 8 and 10 percent next year.",
                "What growth is forecast for the coming year?",
                "",
            ),
        ],
        3,
    ),
    _doc(
        "security_policy",
        "Information Security Policy",
        [
            (
                "Incident reporting",
                "Suspected breaches must be reported to the response team within one hour.",
                "How quickly must a security incident be reported?",
                "",
            ),
            (
                "Access reviews",
                "Privileged accounts are recertified every quarter by their owners.",
                "How often are admin permissions reviewed?",
                "",
            ),
            (
                "Data retention",
                "Audit logs are stored for seven years before secure deletion.",
                "How long are logs kept?",
                "",
            ),
            (
                "Vulnerability patching",
                "Critical fixes are applied within 72 hours of publication.",
                "What is the deadline for installing urgent security updates?",
                "",
            ),
            (
                "Encryption standards",
                "Stored records use AES-256 and traffic uses TLS 1.3 or newer.",
                "Which cryptographic algorithms are required?",
                "",
            ),
            (
                "Vendor assessment",
                "Suppliers are evaluated annually using a 40-question questionnaire.",
                "How are third-party providers checked?",
                "",
            ),
        ],
        4,
    ),
    _doc(
        "lab_methods",
        "Laboratory Methods Manual",
        [
            (
                "Sample storage",
                "Specimens are kept at minus 80 degrees Celsius until analysis.",
                "At what temperature are samples stored?",
                "",
            ),
            (
                "Centrifugation",
                "Tubes are spun at 3,000 rpm for 10 minutes.",
                "How fast and how long should the tubes be spun?",
                "",
            ),
            (
                "Reagent preparation",
                "Buffers are prepared fresh and discarded after 48 hours.",
                "How long can the buffer solutions be used?",
                "",
            ),
            (
                "Calibration interval",
                "Pipettes are checked against reference weights every six months.",
                "How often must pipettes be calibrated?",
                "",
            ),
            (
                "Waste disposal",
                "Biohazard material is placed in red containers and autoclaved.",
                "How should infectious waste be handled?",
                "",
            ),
            (
                "Replicates",
                "Each measurement is repeated in triplicate to estimate variability.",
                "How many times is every measurement repeated?",
                "",
            ),
        ],
        5,
    ),
    _doc(
        "api_guide",
        "Developer API Guide",
        [
            (
                "Pagination",
                "Responses contain at most 50 items and include a cursor for the next page.",
                "How many results does the list endpoint return?",
                "",
            ),
            (
                "Authentication tokens",
                "Bearer credentials expire after 15 minutes and can be renewed with a refresh credential.",
                "How long is an access credential valid?",
                "",
            ),
            (
                "Webhook retries",
                "Failed deliveries are attempted again up to five times with growing delays.",
                "What happens when a webhook delivery fails?",
                "",
            ),
            (
                "Idempotency keys",
                "Repeated submissions with the same key within 24 hours return the original result.",
                "How does the API avoid duplicate submissions?",
                "",
            ),
            (
                "Error format",
                "Problems are reported as JSON containing a code, a message and a request identifier.",
                "What does an error response look like?",
                "",
            ),
            (
                "Versioning",
                "Breaking changes are published under a new major version and announced 90 days ahead.",
                "How are incompatible changes announced?",
                "",
            ),
        ],
        6,
    ),
]


def _wrap(text: str, rng: random.Random) -> list[str]:
    """Greedy wrap at LINE_CHARS; occasionally break a long word with a hyphen at the line end."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= LINE_CHARS:
            current = candidate
            continue
        room = LINE_CHARS - len(current) - 2
        if len(word) >= 9 and room >= 4 and rng.random() < 0.35:
            lines.append(f"{current} {word[:room]}-".strip())
            current = word[room:]
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _squash(text: str) -> str:
    import re

    return re.sub(r"[\s\-]+", "", text.lower())


def _paginate(doc: SourceDoc, lines_per_page: int, seed: int) -> list[list[tuple[str, str]]]:
    rng = random.Random(seed + sum(ord(c) for c in doc.name))  # deterministic (hash() is not)
    body: list[tuple[str, str]] = [("title", doc.title), ("b", "")]
    for heading, paragraphs in doc.sections:
        body.append(("h", heading))
        for paragraph in paragraphs:
            body.extend(("b", ln) for ln in _wrap(paragraph, rng))
            body.append(("b", ""))
    pages: list[list[tuple[str, str]]] = []
    for start in range(0, len(body), lines_per_page):
        chunk = body[start : start + lines_per_page]
        number = len(pages) + 1
        pages.append(
            [("small", f"{doc.title} - Confidential draft"), *chunk, ("small", f"Page {number}")]
        )
    return pages


def straddling_facts(doc: SourceDoc, pages: list[list[tuple[str, str]]]) -> int:
    """How many facts are NOT contained in any single page (they continue on the next page)."""
    texts = [_squash(" ".join(t for st, t in page if st in {"b", "h", "title"})) for page in pages]
    return sum(1 for f in doc.facts if not any(_squash(f.sentence) in t for t in texts))


def build_pages(doc: SourceDoc, *, seed: int = 0) -> list[list[tuple[str, str]]]:
    """Pages with running header/footer. The page size is chosen per document (deterministically)
    to make as many facts as possible continue across a page break."""
    best = max(
        range(30, 47),
        key=lambda n: (straddling_facts(doc, _paginate(doc, n, seed)), -abs(n - 40)),
    )
    return _paginate(doc, best, seed)


def render_pdf(pages: list[list[tuple[str, str]]]) -> bytes:
    """Write a real PDF with Helvetica body text, bold 14pt headings and small header/footer."""
    import io

    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects: list[bytes] = []

    def add(body: str) -> int:
        objects.append(body.encode("latin-1"))
        return len(objects)

    catalog = add("")
    pages_obj = add("")
    f1 = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    f2 = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    page_ids: list[int] = []
    for page in pages:
        ops = ["BT"]
        y = 800.0
        for style, text in page:
            if style == "small":
                size, font, step = 8, "F1", 14
                y_use = 818.0 if text.endswith("draft") else 24.0
                ops.append(f"/{font} {size} Tf 1 0 0 1 56 {y_use} Tm ({esc(text)}) Tj")
                continue
            if style in {"h", "title"}:
                size, font, step = (16, "F2", 24) if style == "title" else (13, "F2", 22)
                y -= 8 if style == "h" else 0
            else:
                size, font, step = 10, "F1", 17
            if text:
                ops.append(f"/{font} {size} Tf 1 0 0 1 56 {y} Tm ({esc(text)}) Tj")
            y -= step
        ops.append("ET")
        stream = "\n".join(ops)
        content = add(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 612 842] "
                f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> /Contents {content} 0 R >>"
            )
        )
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
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def all_facts() -> list[Fact]:
    return [f for d in DOCS for f in d.facts]
