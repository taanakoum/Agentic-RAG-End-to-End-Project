"""document parser column

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08 17:30:02.465368
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('parser', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('documents', 'parser')
