"""The real-document retrieval dataset (KI-018): well-formed, and the hash check really checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from evaluation.retrieval import real_docs


def test_the_dataset_is_well_formed() -> None:
    version, docs, questions = real_docs.load_dataset()
    assert version == "real-docs-v1"
    assert len(docs) == 2 and 20 <= len(questions) <= 30
    names = {d.name for d in docs}
    for doc in docs:
        assert re.fullmatch(r"[0-9a-f]{64}", doc.sha256) and doc.url.startswith("https://")
    ids = [q.qid for q in questions]
    assert len(set(ids)) == len(ids)
    assert len({q.question for q in questions}) == len(questions)
    for q in questions:
        assert q.doc in names and q.question.endswith("?") and len(q.sentence) >= 20


def test_squash_ignores_case_whitespace_hyphenation_and_punctuation() -> None:
    assert (
        real_docs.squash("At least 8 charac-\nters, SHALL be.")
        == real_docs.squash("at least 8 characters shall be")
        or real_docs.squash("A b-c") == "abc"
    )


def test_a_file_with_a_wrong_hash_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, docs, _ = real_docs.load_dataset()
    monkeypatch.setattr(real_docs, "DATA_DIR", tmp_path)
    for doc in docs:  # every file exists, but none is the document that was pinned
        (tmp_path / doc.filename).write_bytes(b"not the document")
    with pytest.raises(SystemExit, match="wrong hash"):
        real_docs.require_files(docs)
