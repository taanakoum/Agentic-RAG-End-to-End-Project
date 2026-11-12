"""Builds services for a given DB session. Used by request dependencies and background tasks."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.container import Infrastructure
from app.services.account_service import AccountService
from app.services.auth_service import AuthService
from app.services.conversation_service import ConversationService
from app.services.document_service import DocumentService
from app.services.feedback_service import FeedbackService
from app.services.memory_service import MemoryService
from app.services.mfa_service import MfaService
from app.services.retrieval_service import RetrievalService


def build_auth_service(session: AsyncSession, infra: Infrastructure) -> AuthService:
    return AuthService(
        session=session,
        settings=infra.settings,
        passwords=infra.passwords,
        limiter=infra.rate_limiter,
    )


def build_account_service(session: AsyncSession, infra: Infrastructure) -> AccountService:
    return AccountService(
        session=session,
        settings=infra.settings,
        passwords=infra.passwords,
        limiter=infra.rate_limiter,
        email=infra.email,
    )


def build_mfa_service(session: AsyncSession, infra: Infrastructure) -> MfaService:
    return MfaService(
        session=session,
        settings=infra.settings,
        passwords=infra.passwords,
        limiter=infra.rate_limiter,
        auth=build_auth_service(session, infra),
    )


def build_document_service(session: AsyncSession, infra: Infrastructure) -> DocumentService:
    return DocumentService(
        session=session,
        settings=infra.settings,
        storage=infra.storage,
        limiter=infra.rate_limiter,
        sync_session_factory=infra.sync_session_factory,
        parser=infra.parser,
        validation_executor=infra.validation_executor,
    )


def build_retrieval_service(session: AsyncSession, infra: Infrastructure) -> RetrievalService:
    return RetrievalService(
        session=session,
        settings=infra.settings,
        qdrant=infra.qdrant,
        executor=infra.inference_executor,
        limiter=infra.rate_limiter,
    )


def build_conversation_service(session: AsyncSession, infra: Infrastructure) -> ConversationService:
    return ConversationService(session=session, settings=infra.settings, limiter=infra.rate_limiter)


def build_memory_service(session: AsyncSession, infra: Infrastructure) -> MemoryService:
    return MemoryService(
        session=session,
        settings=infra.settings,
        limiter=infra.rate_limiter,
        qdrant=infra.qdrant,
        executor=infra.inference_executor,
    )


def build_feedback_service(session: AsyncSession, infra: Infrastructure) -> FeedbackService:
    return FeedbackService(session=session, settings=infra.settings, limiter=infra.rate_limiter)
