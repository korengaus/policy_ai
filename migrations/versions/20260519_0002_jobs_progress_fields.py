"""add Phase 2 M2 progress + result fields to jobs

Revision ID: 20260519_0002
Revises: 20260519_0001
Create Date: 2026-05-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260519_0002"
down_revision: Union[str, None] = "20260519_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("external_id", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("query", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("max_news", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("progress_percent", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("jobs", sa.Column("current_stage", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("result_id", sa.BigInteger(), nullable=True))
    op.add_column("jobs", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_jobs_external_id", "jobs", ["external_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_external_id", table_name="jobs")
    op.drop_column("jobs", "completed_at")
    op.drop_column("jobs", "error_message")
    op.drop_column("jobs", "result_id")
    op.drop_column("jobs", "current_stage")
    op.drop_column("jobs", "progress_percent")
    op.drop_column("jobs", "max_news")
    op.drop_column("jobs", "query")
    op.drop_column("jobs", "external_id")
