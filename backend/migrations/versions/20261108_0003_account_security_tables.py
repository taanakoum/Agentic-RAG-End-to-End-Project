"""account security tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08 16:12:57.438191
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('mfa_recovery_codes',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_mfa_recovery_codes_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mfa_recovery_codes')),
    sa.UniqueConstraint('code_hash', name=op.f('uq_mfa_recovery_codes_code_hash'))
    )
    op.create_index('ix_mfa_recovery_codes_user_id', 'mfa_recovery_codes', ['user_id'], unique=False)
    op.create_table('one_time_tokens',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_one_time_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_one_time_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_one_time_tokens_token_hash'))
    )
    op.create_index('ix_one_time_tokens_user_purpose', 'one_time_tokens', ['user_id', 'purpose'], unique=False)
    op.create_table('user_mfa',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('secret_encrypted', sa.Text(), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_step', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_mfa_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_user_mfa'))
    )
    op.add_column('users', sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'email_verified_at')
    op.drop_table('user_mfa')
    op.drop_index('ix_one_time_tokens_user_purpose', table_name='one_time_tokens')
    op.drop_table('one_time_tokens')
    op.drop_index('ix_mfa_recovery_codes_user_id', table_name='mfa_recovery_codes')
    op.drop_table('mfa_recovery_codes')
