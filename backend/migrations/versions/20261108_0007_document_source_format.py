"""document source format

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08 18:36:38.598677
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('source_format', sa.String(length=8), server_default='pdf', nullable=False))


def downgrade() -> None:
    op.drop_column('documents', 'source_format')
