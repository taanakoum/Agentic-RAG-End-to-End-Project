"""Chunker, text repair and the light parser's OCR rules (pure logic, no services)."""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Any, ClassVar

import pytest

from app.ingestion import sandbox_jobs
from app.ingestion.chunking import (
    chunk_pages,
    contextual_text,
    estimate_tokens,
    heading_level,
    looks_like_heading,
    split_sentences,
)
from app.ingestion.textclean import clean_pages, find_page_furniture
from app.ingestion.textnorm import PageText

# ================================================================ sentence splitting


def test_sentences_split_on_boundaries_but_not_on_abbreviations_or_decimals() -> None:
    text = (
        "Dr. Smith met Mr. Jones at 3.5 percent interest. They agreed (see Fig. 2) on terms. "
        "J. Brown signed e.g. the first page. Done!"
    )
    sentences = split_sentences(text)
    assert sentences[0] == "Dr. Smith met Mr. Jones at 3.5 percent interest."
    assert sentences[1] == "They agreed (see Fig. 2) on terms."
    assert sentences[2] == "J. Brown signed e.g. the first page."
    assert sentences[3] == "Done!"


def test_text_without_terminators_is_one_sentence() -> None:
    assert split_sentences("no punctuation here at all") == ["no punctuation here at all"]


# ================================================================ chunking


def sentences(n: int, topic: str = "subject") -> str:
    return " ".join(
        f"Sentence number {i} explains the {topic} {i % 9} with several additional words attached."
        for i in range(n)
    )


def test_chunks_respect_budget_and_never_cut_sentences() -> None:
    pages = [PageText(1, sentences(80))]
    chunks = chunk_pages(pages, target_tokens=120, max_tokens=180, min_tokens=30, overlap_tokens=20)
    assert len(chunks) > 3
    assert all(c.tokens <= 180 for c in chunks)
    for c in chunks:  # every chunk starts at a sentence start and ends at a sentence end
        assert c.text.startswith("Sentence number")
        assert c.text.rstrip().endswith("attached.")


def test_all_information_is_preserved_and_overlap_is_sentence_level() -> None:
    chunks = chunk_pages(
        [PageText(1, sentences(60))],
        target_tokens=100,
        max_tokens=150,
        min_tokens=30,
        overlap_tokens=30,
    )
    joined = " ".join(c.text for c in chunks)
    for i in range(60):
        assert f"Sentence number {i} " in joined
    shared = [
        re.findall(r"Sentence number (\d+)", a.text)[-1]
        in re.findall(r"Sentence number (\d+)", b.text)
        for a, b in pairwise(chunks)
    ]
    assert any(shared)  # consecutive chunks overlap by whole sentences


def test_chunks_continue_across_page_breaks_and_record_the_page_span() -> None:
    page1 = "The quarterly report states that revenue grew by twelve percent while costs"
    page2 = "fell by four percent compared with the previous year. A new section follows."
    chunks = chunk_pages([PageText(1, page1), PageText(2, page2)])
    assert len(chunks) == 1
    assert "costs fell by four percent" in chunks[0].text.replace(
        "\n", " "
    )  # a sentence split by the break
    assert (chunks[0].page_number, chunks[0].page_end) == (1, 2)


def test_section_paths_follow_heading_levels() -> None:
    text = (
        "1 Introduction\n\n" + sentences(14) + "\n\n"
        "1.1 Background\n\n" + sentences(14, "background") + "\n\n"
        "2 Methods\n\n" + sentences(14, "method")
    )
    chunks = chunk_pages([PageText(1, text)], target_tokens=150, max_tokens=220, min_tokens=40)
    paths = [c.section_path for c in chunks]
    assert "1 Introduction" in paths
    assert "1 Introduction > 1.1 Background" in paths
    assert "2 Methods" in paths  # a new top-level heading pops the previous section
    assert all(not p.startswith("1 Introduction > 2") for p in paths)
    assert chunks[0].heading == "1 Introduction"


def test_a_new_section_starts_a_new_chunk_without_overlap() -> None:
    text = "ALPHA\n\n" + sentences(10, "alpha") + "\n\nBETA\n\n" + sentences(10, "beta")
    chunks = chunk_pages([PageText(1, text)], target_tokens=400, max_tokens=500, min_tokens=40)
    assert len(chunks) == 2
    assert "alpha" not in chunks[1].text.split("BETA", 1)[-1]  # nothing from ALPHA carried over


def test_tiny_sections_are_merged_forward_not_emitted_as_fragments() -> None:
    text = "SHORT\n\nOne tiny line.\n\nNEXT\n\n" + sentences(8)
    chunks = chunk_pages([PageText(1, text)], target_tokens=300, max_tokens=400, min_tokens=48)
    assert len(chunks) == 1
    assert "One tiny line." in chunks[0].text


def test_lists_keep_their_items_whole() -> None:
    text = (
        "Requirements\n\n- first requirement is long enough\n"
        "- second requirement item\n- third one here"
    )
    chunks = chunk_pages([PageText(1, text)], min_chars=10)
    assert chunks
    assert "- second requirement item" in chunks[0].text
    assert "\n- second" in chunks[0].text


def test_oversized_single_sentence_is_split_within_the_hard_limit() -> None:
    long_sentence = " ".join(f"word{i}" for i in range(900)) + "."
    chunks = chunk_pages(
        [PageText(1, long_sentence)], target_tokens=100, max_tokens=150, min_tokens=20
    )
    assert len(chunks) > 3
    assert all(c.tokens <= 150 for c in chunks)
    assert sum(len(c.text.split()) for c in chunks) >= 900


def test_meaningless_pages_are_dropped() -> None:
    pages = [PageText(1, "- - - - - - - - - - - - - - - - - -"), PageText(2, "12"), PageText(3, "")]
    assert chunk_pages(pages, min_chars=30) == []


def test_contextual_text_adds_title_and_section() -> None:
    [chunk] = chunk_pages([PageText(1, "FINANCIAL SUMMARY\n\n" + sentences(6))], min_tokens=10)
    context = contextual_text(chunk, title="Annual Report")
    assert context.startswith("Annual Report > FINANCIAL SUMMARY\n")
    assert chunk.text in context


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("INTRODUCTION", True),
        ("1.2 Related Work", True),
        ("Financial Summary", True),
        ("This is a normal sentence that ends with a period.", False),
        ("a" * 120, False),
        ("see the table below,", False),
        ("ok", False),
    ],
)
def test_heading_detection(line: str, expected: bool) -> None:
    assert looks_like_heading(line) is expected


def test_heading_levels() -> None:
    assert heading_level("1 Intro") == 1
    assert heading_level("1.2 Detail") == 2
    assert heading_level("1.2.3 Deeper") == 3
    assert heading_level("SUMMARY") == 1
    assert heading_level("Plain Title") == 2


def test_token_estimate_is_monotonic() -> None:
    assert estimate_tokens("a b c") < estimate_tokens("a b c d e f g")


# ================================================================ text repair


def test_running_headers_footers_and_page_numbers_are_removed() -> None:
    def body(n: int) -> str:
        return f"Section text number {n} says something genuinely different on this page."

    pages = [
        PageText(
            i, f"ACME Annual Report - Confidential\n{body(i)}\nMore unique content {i}.\nPage {i}"
        )
        for i in range(1, 6)
    ]
    assert "acme annual report - confidential" in find_page_furniture(pages)
    cleaned = clean_pages(pages)
    for page in cleaned:
        assert "ACME Annual Report" not in page.text
        assert not re.search(r"^Page \d", page.text, re.MULTILINE)
        assert "genuinely different" in page.text


def test_furniture_detection_needs_repetition() -> None:
    pages = [PageText(1, "Unique title\nBody one."), PageText(2, "Unique title\nBody two.")]
    assert find_page_furniture(pages) == set()  # fewer than 3 pages: nothing is "repeated"


def test_hyphenated_line_breaks_are_rejoined_and_wrapped_lines_unwrapped() -> None:
    text = (
        "The organisation collected inform-\nation about regional per-\nformance during the year\n"
        "and published the results afterwards."
    )
    [page] = clean_pages([PageText(1, text)])
    assert "information about regional performance during the year and published" in page.text
    assert "\n" not in page.text


def test_real_hyphenated_compounds_and_paragraph_breaks_survive() -> None:
    text = (
        "A well-known result holds.\n\n"
        "The second paragraph starts here and\ncontinues on the next line."
    )
    [page] = clean_pages([PageText(1, text)])
    assert "well-known" in page.text
    assert "\n\n" in page.text
    assert "starts here and continues" in page.text


def test_list_items_are_not_merged() -> None:
    text = "Items:\n- first item here\n- second item here\n- third item here"
    [page] = clean_pages([PageText(1, text)])
    assert page.text.count("\n- ") == 3  # a line break before each of the three items
    assert page.text.splitlines()[1:] == [
        "- first item here",
        "- second item here",
        "- third item here",
    ]


# ============================================================ light parser OCR rules (child code)


class FakeEngine:
    instances: ClassVar[list[FakeEngine]] = []

    def __init__(self) -> None:
        self.calls: list[int] = []
        FakeEngine.instances.append(self)

    def ocr_pdf_page(self, data: bytes, index: int, *, dpi: int) -> str:
        self.calls.append(index)
        return f"Recovered text from scanned page number {index + 1}"


def light(monkeypatch: pytest.MonkeyPatch, page_texts: list[str], **opts: Any) -> dict[str, Any]:
    monkeypatch.setattr(sandbox_jobs, "_text_layer", _layer_of(page_texts))
    monkeypatch.setattr("app.ingestion.ocr.RapidOcrEngine", FakeEngine)
    monkeypatch.setattr("builtins.open", _fake_open)
    FakeEngine.instances.clear()
    options = {
        "min_text_chars_per_page": 25,
        "ocr_enabled": True,
        "ocr_max_pages": 40,
        "ocr_dpi": 100,
        **opts,
    }
    return sandbox_jobs.cmd_light("ignored.pdf", options)


def _layer_of(texts: list[str]) -> Any:
    def layer(path: str) -> list[str]:
        return list(texts)

    return layer


def _fake_open(*args: Any, **kwargs: Any) -> Any:
    import io

    return io.BytesIO(b"%PDF-")


def test_ocr_is_never_started_when_every_page_has_text(monkeypatch: pytest.MonkeyPatch) -> None:
    result = light(
        monkeypatch, ["A page with plenty of genuine extractable text in the layer."] * 3
    )
    assert result["ocr_status"] == "not_needed"
    assert result["ocr_pages"] == 0
    assert FakeEngine.instances == []  # the OCR engine was not even constructed


def test_ocr_runs_only_for_pages_without_text(monkeypatch: pytest.MonkeyPatch) -> None:
    result = light(monkeypatch, ["Page one has plenty of real text in its text layer.", "", " 12 "])
    assert FakeEngine.instances[0].calls == [1, 2]
    assert result["ocr_status"] == "used"
    assert result["ocr_pages"] == 2
    assert [p["from_ocr"] for p in result["pages"]] == [False, True, True]


def test_ocr_disabled_leaves_scanned_pages_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    result = light(monkeypatch, [""], ocr_enabled=False)
    assert result["ocr_status"] == "disabled"
    assert FakeEngine.instances == []


def test_ocr_page_limit_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    result = light(monkeypatch, [""] * 5, ocr_max_pages=2)
    assert FakeEngine.instances[0].calls == [0, 1]
    assert result["ocr_status"] == "limited"
    assert result["ocr_pages"] == 2


def test_ocr_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(FakeEngine):
        def ocr_pdf_page(self, data: bytes, index: int, *, dpi: int) -> str:
            raise RuntimeError("model failed")

    monkeypatch.setattr(sandbox_jobs, "_text_layer", _layer_of(["", ""]))
    monkeypatch.setattr("app.ingestion.ocr.RapidOcrEngine", Broken)
    monkeypatch.setattr("builtins.open", _fake_open)
    result = sandbox_jobs.cmd_light(
        "x.pdf",
        {"min_text_chars_per_page": 25, "ocr_enabled": True, "ocr_max_pages": 40, "ocr_dpi": 100},
    )
    assert result["ocr_status"] == "failed"
    assert result["ocr_pages"] == 0
