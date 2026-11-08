"""Document lifecycle state machine. Illegal transitions raise; ALLOWED is the contract."""

from __future__ import annotations

from enum import StrEnum


class DocumentStatus(StrEnum):
    PENDING = "pending"
    VALIDATING = "validating"
    QUEUED = "queued"
    PROCESSING = "processing"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"


S = DocumentStatus

#: Stages in which a worker is actively doing work for a document.
ACTIVE_STAGES = frozenset({S.PROCESSING, S.EXTRACTING, S.CHUNKING, S.EMBEDDING, S.INDEXING})

_FORWARD: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    S.PENDING: frozenset({S.VALIDATING, S.FAILED}),
    S.VALIDATING: frozenset({S.QUEUED, S.FAILED}),
    S.QUEUED: frozenset({S.PROCESSING, S.FAILED}),
    S.PROCESSING: frozenset({S.EXTRACTING}),
    S.EXTRACTING: frozenset({S.CHUNKING}),
    S.CHUNKING: frozenset({S.EMBEDDING}),
    S.EMBEDDING: frozenset({S.INDEXING}),
    S.INDEXING: frozenset({S.COMPLETED}),
    S.COMPLETED: frozenset({S.QUEUED}),  # explicit re-index only
    S.FAILED: frozenset({S.QUEUED}),  # explicit retry only
}

ALLOWED: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    state: targets
    | (
        # any active stage can fail, or be sent back to the queue by crash recovery
        frozenset({S.FAILED, S.QUEUED}) if state in ACTIVE_STAGES else frozenset()
    )
    for state, targets in _FORWARD.items()
}


class IllegalTransitionError(Exception):
    def __init__(self, current: DocumentStatus, target: DocumentStatus) -> None:
        super().__init__(f"illegal document transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


def check_transition(current: DocumentStatus, target: DocumentStatus) -> None:
    if target not in ALLOWED[current]:
        raise IllegalTransitionError(current, target)


def is_retrievable(status: DocumentStatus) -> bool:
    """Only completed documents may be searched."""
    return status is DocumentStatus.COMPLETED
