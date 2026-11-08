"""Memory logic that needs no services: sensitivity filter, extraction rules, identity hash."""

from __future__ import annotations

import pytest

from app.memory.extract import ConversationTurn, RuleExtractor
from app.memory.sensitive import sensitive_reason
from app.repositories.memory import content_hash

# ------------------------------------------------------------------ sensitivity filter


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("my password is hunter2", "credential"),
        ("The API key is stored in the vault", "credential"),
        ("use token abc for staging", "credential"),
        ("key sk-ABCDEF0123456789abcdef01 works", "api_key"),
        ("ghp_ABCDEF0123456789abcdef0123", "api_key"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key"),
        ("write to dana.smith@example.com", "email"),
        ("call me on +1 (415) 555-0132", "phone"),
        ("ssn 123-45-6789", "ssn"),
        ("pay to GB82WEST12345698765432", "iban"),
        ("connect via https://admin:s3cret@db.internal/x", "url_credentials"),
        ("card 4111 1111 1111 1111 expires soon", "payment_card"),
        ("card 4111-1111-1111-1111", "payment_card"),
    ],
)
def test_sensitive_text_is_recognised(text: str, reason: str) -> None:
    assert sensitive_reason(text) == reason


@pytest.mark.parametrize(
    "text",
    [
        "The user prefers dark mode.",
        "The user works at Acme Corporation.",
        "Deploys happen on Fridays at 16:00.",
        "The user is a data engineer.",
        "Order number 12345 was delayed.",  # a short number is not a card
        "Version 3.13.12 is pinned.",
    ],
)
def test_ordinary_text_is_not_flagged(text: str) -> None:
    assert sensitive_reason(text) is None


def test_card_shaped_numbers_that_fail_the_checksum_are_still_flagged_as_numbers() -> None:
    assert sensitive_reason("1234 5678 9012 3456") == "phone"  # conservative, see docstring


# ------------------------------------------------------------------ extraction rules


def extract(*messages: str, role: str = "user") -> list[str]:
    return [
        c.content for c in RuleExtractor().extract([ConversationTurn(role, m) for m in messages])
    ]


def test_explicit_statements_become_memories() -> None:
    found = extract(
        "Remember that I deploy on Fridays.",
        "My name is Dana.",
        "I prefer concise answers.",
        "I work at Acme Corp.",
        "I'm a data engineer.",
        "I usually use Python.",
        "I don't like long meetings.",
    )
    assert found == [
        "I deploy on Fridays.",
        "The user's name is Dana.",
        "The user prefers concise answers.",
        "The user works at Acme Corp.",
        "The user is a data engineer.",
        "The user usually uses Python.",
        "The user dislikes long meetings.",
    ]


def test_questions_and_assistant_text_are_ignored() -> None:
    assert extract("Do you remember that I like tea?", "What is my name?") == []
    assert extract("My name is Dana.", "I prefer tea.", role="assistant") == []
    assert extract("Should I prefer tabs?") == []


def test_only_the_statement_sentence_is_used_not_the_whole_message() -> None:
    found = extract("Hi there! My name is Dana. Can you summarise the report?")
    assert found == ["The user's name is Dana."]


def test_sensitive_statements_are_never_extracted() -> None:
    assert extract("Remember that my password is hunter2.") == []
    assert extract("Remember my email dana@example.com for later.") == []
    assert extract("Remember that my card is 4111 1111 1111 1111.") == []
    assert extract("Remember that I prefer dark mode.") == ["I prefer dark mode."]


def test_duplicates_within_a_batch_and_oversized_statements_are_dropped() -> None:
    assert extract("I prefer tea.", "i prefer tea.") == ["The user prefers tea."]
    assert extract("Remember that " + "x" * 600) == []


def test_extracted_memories_carry_kind_and_importance() -> None:
    (candidate,) = RuleExtractor().extract([ConversationTurn("user", "My name is Dana.")])
    assert (candidate.kind, candidate.importance) == ("semantic", 5)


# ------------------------------------------------------------------ identity


def test_content_hash_ignores_case_and_spacing_but_not_meaning() -> None:
    assert content_hash("The user prefers  dark mode.") == content_hash(
        "the USER prefers dark mode."
    )
    assert content_hash("prefers dark mode") != content_hash("prefers light mode")
