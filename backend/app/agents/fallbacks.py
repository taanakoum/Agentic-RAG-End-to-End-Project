"""Answers written by the server (never by a model): smalltalk, clarification, no evidence.

They cost no model call, never hallucinate and are the safe landing place whenever generation or
verification cannot produce a trustworthy cited answer.
"""

from __future__ import annotations

SMALLTALK: dict[str, str] = {  # keys are SmalltalkKind values
    "greeting": (
        "Hello! Ask me a question about your uploaded documents and I will answer from them, "
        "with citations."
    ),
    "thanks": "You're welcome! Ask me anything else about your documents.",
    "bye": "Goodbye! Your conversation is saved, so you can continue it any time.",
    "capabilities": (
        "I answer questions using the documents you have uploaded (PDF, DOCX, TXT, Markdown, CSV). "
        "Every answer cites the passages it is based on. If your documents do not contain the "
        "answer I will say so instead of guessing."
    ),
    "wellbeing": "I'm ready to help. What would you like to know about your documents?",
}

CLARIFY = (
    "I could not tell what you would like to know. Could you rephrase your question and mention "
    "what you are looking for in your documents?"
)

NO_EVIDENCE = (
    "I could not find anything about this in your documents, so I cannot answer it reliably. "
    "Try different wording, or check that the relevant document has finished processing."
)

NO_DOCUMENTS = (
    "You have no completed documents yet. Upload a document and wait until it is processed, "
    "then ask your question again."
)

UNVERIFIED = (
    "I could not produce an answer that I can back with citations from your documents, so I am "
    "not giving one. Please rephrase the question or ask about a more specific passage."
)

CLARIFY_FOLLOW_UPS = ["Which document or topic are you asking about?"]
