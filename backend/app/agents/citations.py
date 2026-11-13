"""Server-side citation verification (docs/spec/04 "Citations").

The model proposes `[S1]`-style citations over the sources shown to it. Nothing the model writes
is trusted. For every proposed citation the server checks that:
  1. the source label is one of the sources retrieved for THIS execution (the authorized
     evidence set: unknown labels, other users' ids and invented ids cannot match);
  2. the chunk id is consistent with its document, version and index (`point_id`), i.e. the chunk
     belongs to the document it claims to;
  3. the document still exists, belongs to the user, is completed, and is at the version that was
     retrieved (PostgreSQL is re-asked, so a deletion or re-index mid-turn invalidates it);
  4. if the model named a page, it lies within the stored page range of the chunk;
  5. the quoted words really occur in the stored chunk text. The excerpt shown to the user is
     TAKEN FROM THE STORED TEXT (the matching sentence), never copied from the model.
`document_name`, `page_number`, `chunk_id` and `document_id` of every `Citation` come from stored
metadata. Invalid citations are dropped; a factual answer that ends up with none is reported as a
problem so the graph can repair once and then fall back to a safe answer.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import Citation, DraftAnswer
from app.ingestion.chunking import split_sentences
from app.ingestion.indexing import point_id

MAX_EXCERPT_CHARS = 300
MAX_CITATIONS = 8
MIN_QUOTE_CHARS = 12
# One marker may group several labels: [S1], [S1, S2], [S1; S3] (models do group them; found by the
# real-document browser tour, where "[S1, S2]" reached the user unconverted).
_LABEL = re.compile(r"S[0-9]{1,3}")
_MARKER = re.compile(r"\s?\[\s*(S[0-9]{1,3}(?:\s*[,;]\s*S[0-9]{1,3})*)\s*\]")
_FUZZY_MIN_OVERLAP = 0.6


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One retrieved source as shown to the model (metadata only plus the stored text)."""

    label: str
    chunk_id: str
    document_id: uuid.UUID
    document_version: int
    chunk_index: int
    page_number: int | None
    page_end: int | None
    text: str
    document_name: str


@dataclass(slots=True)
class VerificationResult:
    answer: str
    citations: list[Citation]
    dropped: list[str] = field(default_factory=list[str])
    corrected: int = 0
    problem: str | None = None  # e.g. "no_valid_citations"

    @property
    def ok(self) -> bool:
        return self.problem is None


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _truncate(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    cut = text[:MAX_EXCERPT_CHARS].rsplit(" ", 1)[0]
    return cut + "..."


def derive_excerpt(quote: str, stored_text: str) -> tuple[str, bool] | None:
    """The stored sentence that contains `quote`. Returns (excerpt, exact) or None.

    exact=False means the quote was only approximately present (token overlap >= 60%): the
    citation is kept but corrected to the real stored sentence.
    """
    wanted = _norm(quote)
    # a single word or a couple of characters proves nothing ("25", "the gateway"[:3] ...)
    if len(wanted) < MIN_QUOTE_CHARS or len(wanted.split()) < 2:
        return None
    sentences = [s for s in split_sentences(stored_text) if s.strip()] or [stored_text]
    for sentence in sentences:  # one sentence first: the tightest stored evidence wins
        if wanted in _norm(sentence):
            return _truncate(sentence), True
    for index in range(len(sentences) - 1):  # then a quote that spans two sentences
        joined = f"{sentences[index]} {sentences[index + 1]}"
        if wanted in _norm(joined):
            return _truncate(joined), True
    if wanted in _norm(stored_text):
        return _truncate(stored_text), True
    wanted_tokens = set(re.findall(r"\w+", wanted))
    if len(wanted_tokens) < 3:
        return None
    best: tuple[float, str] = (0.0, "")
    for sentence in sentences:
        tokens = set(re.findall(r"\w+", _norm(sentence)))
        overlap = len(wanted_tokens & tokens) / len(wanted_tokens)
        if overlap > best[0]:
            best = (overlap, sentence)
    if best[0] >= _FUZZY_MIN_OVERLAP:
        return _truncate(best[1]), False
    return None


def verify_draft(
    draft: DraftAnswer,
    evidence: Mapping[str, EvidenceItem],
    readable: Mapping[uuid.UUID, tuple[str, int]],
) -> VerificationResult:
    """Pure verification (no I/O). `readable` is id -> (filename, version) from PostgreSQL."""
    citations: list[Citation] = []
    verified_labels: list[str] = []
    seen_chunks: set[str] = set()
    dropped: list[str] = []
    corrected = 0
    for proposed in draft.citations:
        item = evidence.get(proposed.source)
        if item is None:
            dropped.append("unknown_source")
            continue
        if item.chunk_id in seen_chunks:
            continue  # the same chunk cited twice counts once
        if point_id(item.document_id, item.document_version, item.chunk_index) != item.chunk_id:
            dropped.append("chunk_document_mismatch")
            continue
        stored = readable.get(item.document_id)
        if stored is None or stored[1] != item.document_version:
            dropped.append("document_unavailable")
            continue
        if proposed.page is not None:
            first = item.page_number if item.page_number is not None else proposed.page
            last = item.page_end if item.page_end is not None else first
            if not first <= proposed.page <= last:
                dropped.append("wrong_page")
                continue
        derived = derive_excerpt(proposed.quote, item.text)
        if derived is None:
            dropped.append("unsupported_quote")
            continue
        excerpt, exact = derived
        corrected += 0 if exact else 1
        seen_chunks.add(item.chunk_id)
        verified_labels.append(proposed.source)
        citations.append(
            Citation(
                document_id=str(item.document_id),
                document_name=stored[0],  # PostgreSQL's name, not a prompt-supplied one
                page_number=item.page_number,
                chunk_id=item.chunk_id,
                excerpt=excerpt,
            )
        )
        if len(citations) >= MAX_CITATIONS:
            break

    answer, ordered = _renumber(draft.answer, verified_labels)
    by_label = dict(zip(verified_labels, citations, strict=True))
    final = [by_label[label] for label in ordered]
    needs_citations = not (
        draft.insufficient_evidence or draft.clarification_needed or draft.support == "not_found"
    )
    problem = "no_valid_citations" if needs_citations and not final else None
    return VerificationResult(answer, final, dropped, corrected, problem)


def _renumber(answer: str, verified: Sequence[str]) -> tuple[str, list[str]]:
    """Replace [S3]-style markers by [1], [2], ... in order of first appearance; drop the rest.

    A grouped marker such as [S1, S4] becomes [1, 2]; unverified labels inside it are dropped, and
    a marker with no verified label disappears.
    """
    order: list[str] = []
    for match in _MARKER.finditer(answer):
        for label in _LABEL.findall(match.group(1)):
            if label in verified and label not in order:
                order.append(label)
    order += [label for label in verified if label not in order]
    number = {label: index + 1 for index, label in enumerate(order)}

    def replace(match: re.Match[str]) -> str:
        numbers: list[int] = []
        for label in _LABEL.findall(match.group(1)):
            if label in number and number[label] not in numbers:
                numbers.append(number[label])
        return f" [{', '.join(map(str, numbers))}]" if numbers else ""

    return _MARKER.sub(replace, answer).strip(), order


async def verify_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    draft: DraftAnswer,
    evidence: Mapping[str, EvidenceItem],
) -> VerificationResult:
    """Ask PostgreSQL which of the cited documents the user may still read, then verify."""
    from app.repositories.documents import DocumentRepository

    readable = await DocumentRepository(session).readable(
        user_id, [item.document_id for item in evidence.values()]
    )
    return verify_draft(draft, evidence, readable)
