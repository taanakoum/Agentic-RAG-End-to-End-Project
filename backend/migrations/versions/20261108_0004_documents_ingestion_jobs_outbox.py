"""documents ingestion jobs outbox

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-08 16:32:04.271265
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('outbox_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outbox_events'))
    )
    op.create_index('ix_outbox_events_unsent', 'outbox_events', ['id'], unique=False, postgresql_where=sa.text('sent_at IS NULL'))
    op.create_table('documents',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('mime_type', sa.String(length=100), nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('error_recoverable', sa.Boolean(), nullable=True),
    sa.Column('page_count', sa.Integer(), nullable=True),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('ocr_status', sa.String(length=16), server_default='not_needed', nullable=False),
    sa.Column('ocr_pages', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('chunk_count', sa.Integer(), nullable=True),
    sa.Column('embedding_model', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('pending', 'validating', 'queued', 'processing', 'extracting', 'chunking', 'embedding', 'indexing', 'completed', 'failed')", name=op.f('ck_documents_status_valid')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_documents_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents'))
    )
    op.create_index('ix_documents_deleted_at', 'documents', ['deleted_at'], unique=False, postgresql_where=sa.text('deleted_at IS NOT NULL'))
    op.create_index('ix_documents_user_created', 'documents', ['user_id', 'created_at'], unique=False)
    op.create_index('uq_documents_user_content_hash_live', 'documents', ['user_id', 'content_hash'], unique=True, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_table('ingestion_jobs',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('queued', 'processing', 'succeeded', 'failed', 'superseded')", name=op.f('ck_ingestion_jobs_status_valid')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_ingestion_jobs_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_ingestion_jobs_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ingestion_jobs'))
    )
    op.create_index('ix_ingestion_jobs_document', 'ingestion_jobs', ['document_id'], unique=False)
    op.create_index('ix_ingestion_jobs_status_updated', 'ingestion_jobs', ['status', 'updated_at'], unique=False)
    op.create_index('ix_ingestion_jobs_user', 'ingestion_jobs', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_ingestion_jobs_user', table_name='ingestion_jobs')
    op.drop_index('ix_ingestion_jobs_status_updated', table_name='ingestion_jobs')
    op.drop_index('ix_ingestion_jobs_document', table_name='ingestion_jobs')
    op.drop_table('ingestion_jobs')
    op.drop_index('uq_documents_user_content_hash_live', table_name='documents', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_index('ix_documents_user_created', table_name='documents')
    op.drop_index('ix_documents_deleted_at', table_name='documents', postgresql_where=sa.text('deleted_at IS NOT NULL'))
    op.drop_table('documents')
    op.drop_index('ix_outbox_events_unsent', table_name='outbox_events', postgresql_where=sa.text('sent_at IS NULL'))
    op.drop_table('outbox_events')
