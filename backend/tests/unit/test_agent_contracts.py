"""Response contracts, JSON parsing and the citation verifier (pure logic, no services)."""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from app.agents.citations import EvidenceItem, VerificationResult, derive_excerpt, verify_draft
from app.agents.schemas import (
    AgentMetadata,
    Citation,
    DraftAnswer,
    DraftCitation,
    RAGResponse,
    draft_json_schema,
)
from app.ingestion.indexing import point_id
from app.llm.structured import extract_json_object, parse_model_json

DOC = uuid.uuid4()
OTHER_DOC = uuid.uuid4()
TEXT_1 = (
    "The gateway rejects uploads. Files larger than 25 megabytes are rejected by the gateway. "
    "Smaller files are accepted."
)
TEXT_2 = "Idle sessions are closed automatically after 30 minutes. Admins can change this."


def item(
    label: str = "S1",
    *,
    document: uuid.UUID = DOC,
    version: int = 1,
    index: int = 0,
    page: int | None = 3,
    page_end: int | None = 4,
    text: str = TEXT_1,
    name: str = "stale-name.pdf",
    chunk_id: str | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        label=label,
        chunk_id=chunk_id or point_id(document, version, index),
        document_id=document,
        document_version=version,
        chunk_index=index,
        page_number=page,
        page_end=page_end,
        text=text,
        document_name=name,
    )


EVIDENCE = {
    "S1": item("S1", index=0, text=TEXT_1),
    "S2": item("S2", index=1, page=7, page_end=7, text=TEXT_2),
}
READABLE = {DOC: ("handbook.pdf", 1)}


def draft(answer: str, *citations: DraftCitation, **kwargs: object) -> DraftAnswer:
    return DraftAnswer(answer=answer, citations=list(citations), **kwargs)  # pyright: ignore[reportArgumentType]


def cite(source: str, quote: str, page: int | None = None) -> DraftCitation:
    return DraftCitation(source=source, quote=quote, page=page)


# ------------------------------------------------------------------ contracts


def test_the_response_contract_rejects_unknown_fields_and_is_versioned() -> None:
    meta = AgentMetadata(
        execution_mode="single", model_name="m", retrieval_used=True, reranking_used=True
    )
    response = RAGResponse(answer="a", metadata=meta)
    assert response.schema_version == "1.0" and response.citations == []
    with pytest.raises(ValidationError):
        RAGResponse(answer="a", metadata=meta, surprise=1)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError):
        Citation.model_validate(
            {
                "document_id": "d",
                "document_name": "n",
                "chunk_id": "c",
                "excerpt": "e",
                "score": 0.9,
            }
        )
    with pytest.raises(ValidationError):
        AgentMetadata.model_validate(
            {
                "execution_mode": "swarm",
                "model_name": "m",
                "retrieval_used": True,
                "reranking_used": True,
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"answer": ""},
        {"answer": "x" * 8001},
        {"answer": "ok", "extra": 1},
        {"answer": "ok", "citations": [{"source": "chunk-1", "quote": "q"}]},
        {"answer": "ok", "citations": [{"source": "S1", "quote": ""}]},
        {"answer": "ok", "citations": [{"source": "S1", "quote": "q", "chunk_id": "x"}]},
        {"answer": "ok", "citations": [{"source": "S1", "quote": "q", "page": 0}]},
        {"answer": "ok", "follow_up_questions": ["a", "b", "c", "d"]},
        {"answer": "ok", "support": "certain"},
        {"answer": "ok", "insufficient_evidence": "maybe"},
    ],
)
def test_the_draft_contract_rejects_malformed_model_output(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DraftAnswer.model_validate(payload)


def test_the_draft_schema_is_exposed_for_native_structured_output() -> None:
    schema = draft_json_schema()
    assert schema["type"] == "object" and "answer" in schema["properties"]  # pyright: ignore[reportOperatorIssue]


# ------------------------------------------------------------------ JSON extraction


def test_plain_fenced_and_prose_wrapped_json_are_all_accepted() -> None:
    body = {"answer": "hello [S1]", "citations": [{"source": "S1", "quote": "hello"}]}
    plain = json.dumps(body)
    for text in (plain, f"```json\n{plain}\n```", f"Sure! Here you go:\n{plain}\nHope it helps."):
        parsed = parse_model_json(text, DraftAnswer)
        assert parsed.ok and parsed.value is not None and parsed.value.answer == "hello [S1]"


def test_braces_inside_strings_do_not_confuse_the_extractor() -> None:
    text = 'noise {"answer": "use {curly} and \\"quotes\\" }", "support": "inference"} tail {x'
    parsed = parse_model_json(text, DraftAnswer)
    assert parsed.ok and parsed.value is not None
    assert (
        parsed.value.answer == 'use {curly} and "quotes" }' and parsed.value.support == "inference"
    )


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("I cannot help with that.", "no_json_object"),
        ('{"answer": "unterminated', "no_json_object"),
        ("{'answer': 'single quotes'}", "malformed_json"),
        ('{"answer": "trailing comma",}', "malformed_json"),
        ("x" * 100_001, "too_long"),
    ],
)
def test_text_that_is_not_json_is_rejected_with_a_safe_hint(text: str, hint: str) -> None:
    parsed = parse_model_json(text, DraftAnswer)
    assert not parsed.ok and hint in parsed.errors[0]


def test_validation_hints_name_fields_not_values() -> None:
    secret = "TOP-SECRET-VALUE-123"
    text = json.dumps({"answer": secret, "citations": [{"source": "bad label", "quote": secret}]})
    parsed = parse_model_json(text, DraftAnswer)
    assert not parsed.ok
    joined = " | ".join(parsed.errors)
    assert "citations.0.source" in joined and secret not in joined


def test_missing_and_extra_fields_are_reported_by_path() -> None:
    assert parse_model_json('{"citations": []}', DraftAnswer).errors == ("answer: missing",)
    extra = parse_model_json('{"answer": "a", "debug": true}', DraftAnswer)
    assert extra.errors == ("debug: extra_forbidden",)


def test_extract_json_object_handles_nesting_and_absence() -> None:
    assert extract_json_object('a {"x": {"y": [1, {"z": 2}]}} b') == '{"x": {"y": [1, {"z": 2}]}}'
    assert extract_json_object("no braces") is None and extract_json_object("}{") is None


# ------------------------------------------------------------------ excerpts


def test_the_excerpt_is_the_stored_sentence_not_the_models_wording() -> None:
    found = derive_excerpt("larger than 25 megabytes", TEXT_1)
    assert found == ("Files larger than 25 megabytes are rejected by the gateway.", True)
    # case and spacing do not matter, the STORED text is returned
    found = derive_excerpt("FILES   LARGER than 25 MEGABYTES", TEXT_1)
    assert found is not None and found[0].startswith("Files larger")


def test_a_quote_spanning_two_sentences_returns_both_stored_sentences() -> None:
    found = derive_excerpt("rejected by the gateway. Smaller files are accepted", TEXT_1)
    assert found is not None and found[1] is True
    assert "rejected by the gateway." in found[0] and "Smaller files are accepted." in found[0]


def test_an_approximate_quote_is_corrected_to_the_stored_sentence() -> None:
    found = derive_excerpt("files bigger than 25 megabytes are rejected by the gateway", TEXT_1)
    assert found == ("Files larger than 25 megabytes are rejected by the gateway.", False)


@pytest.mark.parametrize(
    "quote",
    ["The limit is 50 gigabytes.", "completely unrelated words here", "", "gateway"[:0], "25"],
)
def test_quotes_that_are_not_in_the_source_are_refused(quote: str) -> None:
    assert derive_excerpt(quote, TEXT_1) is None


def test_very_long_stored_sentences_are_truncated() -> None:
    long_text = "word " * 400
    excerpt = derive_excerpt("word word word", long_text)
    assert excerpt is not None and len(excerpt[0]) <= 303 and excerpt[0].endswith("...")


# ------------------------------------------------------------------ verification


def verify(
    d: DraftAnswer,
    evidence: dict[str, EvidenceItem] | None = None,
    readable: dict[uuid.UUID, tuple[str, int]] | None = None,
) -> VerificationResult:
    return verify_draft(
        d, EVIDENCE if evidence is None else evidence, READABLE if readable is None else readable
    )


def test_a_valid_citation_is_built_from_stored_metadata_only() -> None:
    result = verify(
        draft("Uploads are limited to 25 MB [S1].", cite("S1", "larger than 25 megabytes")),
    )
    assert result.ok and result.dropped == [] and result.corrected == 0
    (citation,) = result.citations
    assert citation.document_id == str(DOC) and citation.chunk_id == EVIDENCE["S1"].chunk_id
    assert citation.document_name == "handbook.pdf"  # PostgreSQL's name, not the evidence copy
    assert citation.page_number == 3  # stored page, never the model's
    assert citation.excerpt == "Files larger than 25 megabytes are rejected by the gateway."
    assert result.answer == "Uploads are limited to 25 MB [1]."


def test_markers_are_numbered_in_order_of_appearance_and_unverified_markers_vanish() -> None:
    result = verify(
        draft(
            "Sessions expire [S2] but uploads are capped [S1]. Also something else [S9] and [S1].",
            cite("S1", "larger than 25 megabytes"),
            cite("S2", "closed automatically after 30 minutes"),
            cite("S9", "invented"),
        )
    )
    assert (
        result.answer
        == "Sessions expire [1] but uploads are capped [2]. Also something else and [2]."
    )
    assert [c.chunk_id for c in result.citations] == [
        EVIDENCE["S2"].chunk_id,
        EVIDENCE["S1"].chunk_id,
    ]
    assert result.dropped == ["unknown_source"]


def test_a_fabricated_source_label_is_dropped_and_leaves_a_problem() -> None:
    result = verify(draft("It is 50 GB [S7].", cite("S7", "limit is 50 gigabytes")))
    assert result.citations == [] and result.dropped == ["unknown_source"]
    assert result.problem == "no_valid_citations" and not result.ok


def test_a_chunk_id_that_does_not_belong_to_its_document_is_dropped() -> None:
    forged = {"S1": item("S1", chunk_id=point_id(OTHER_DOC, 1, 0))}  # chunk of ANOTHER document
    result = verify(draft("Yes [S1].", cite("S1", "larger than 25 megabytes")), evidence=forged)
    assert result.dropped == ["chunk_document_mismatch"] and result.problem == "no_valid_citations"
    garbage = {"S1": item("S1", chunk_id="not-a-real-id")}
    assert verify(
        draft("Yes.", cite("S1", "larger than 25 megabytes")), evidence=garbage
    ).dropped == ["chunk_document_mismatch"]


def test_a_document_that_is_deleted_foreign_or_reindexed_invalidates_the_citation() -> None:
    d = draft("Yes [S1].", cite("S1", "larger than 25 megabytes"))
    assert verify(d, readable={}).dropped == ["document_unavailable"]  # deleted / not the user's
    assert verify(d, readable={DOC: ("handbook.pdf", 2)}).dropped == [
        "document_unavailable"
    ]  # re-indexed
    assert verify(d, readable={OTHER_DOC: ("x.pdf", 1)}).dropped == ["document_unavailable"]


def test_the_page_must_match_stored_metadata() -> None:
    quote = cite("S1", "larger than 25 megabytes", page=3)
    assert verify(draft("Yes [S1].", quote)).ok
    assert verify(draft("Yes [S1].", cite("S1", "larger than 25 megabytes", page=4))).ok  # page_end
    wrong = verify(draft("Yes [S1].", cite("S1", "larger than 25 megabytes", page=9)))
    assert wrong.dropped == ["wrong_page"] and wrong.problem == "no_valid_citations"
    no_claim = verify(draft("Yes [S1].", cite("S1", "larger than 25 megabytes")))
    assert no_claim.ok  # a model that names no page is fine: the stored page is used


def test_a_quote_that_is_not_in_the_source_is_dropped() -> None:
    result = verify(draft("Yes [S1].", cite("S1", "the maximum is one terabyte for everyone")))
    assert result.dropped == ["unsupported_quote"] and result.problem == "no_valid_citations"


def test_one_bad_citation_does_not_invalidate_the_good_ones() -> None:
    result = verify(
        draft(
            "A [S1] B [S2].",
            cite("S1", "invented words that do not exist anywhere"),
            cite("S2", "closed automatically after 30 minutes"),
        )
    )
    assert result.ok and len(result.citations) == 1 and result.dropped == ["unsupported_quote"]
    assert result.citations[0].chunk_id == EVIDENCE["S2"].chunk_id
    assert result.answer == "A B [1]."


def test_the_same_chunk_cited_twice_counts_once() -> None:
    result = verify(
        draft(
            "A [S1]. B [S1].",
            cite("S1", "larger than 25 megabytes"),
            cite("S1", "Smaller files are accepted"),
        )
    )
    assert len(result.citations) == 1 and result.answer == "A [1]. B [1]."


def test_approximate_quotes_are_kept_but_counted_as_corrected() -> None:
    result = verify(
        draft("Yes [S1].", cite("S1", "files bigger than 25 megabytes are rejected by the gateway"))
    )
    assert result.ok and result.corrected == 1
    assert (
        result.citations[0].excerpt == "Files larger than 25 megabytes are rejected by the gateway."
    )


@pytest.mark.parametrize(
    "flags",
    [
        {"insufficient_evidence": True},
        {"clarification_needed": True},
        {"support": "not_found"},
    ],
)
def test_answers_that_do_not_claim_facts_need_no_citations(flags: dict[str, object]) -> None:
    result = verify(draft("The documents do not say.", **flags))
    assert result.ok and result.citations == [] and result.problem is None


def test_a_factual_answer_without_any_citation_is_a_problem() -> None:
    result = verify(draft("The limit is 25 MB."))
    assert result.problem == "no_valid_citations"
    inference = verify(draft("Probably 25 MB.", support="inference"))
    assert inference.problem == "no_valid_citations"  # an inference must still point at a source


def test_at_most_eight_citations_are_returned() -> None:
    evidence = {
        f"S{n}": item(f"S{n}", index=n, text=f"Fact number {n} is stored here.")
        for n in range(1, 13)
    }
    d = draft(
        " ".join(f"[S{n}]" for n in range(1, 13)),
        *[cite(f"S{n}", f"Fact number {n} is stored") for n in range(1, 13)],
    )
    assert len(verify(d, evidence=evidence).citations) == 8
