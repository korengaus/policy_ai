"""initial dual-write schema (stories, claims, verdicts, jobs, audit_log)

Revision ID: 20260519_0001
Revises:
Create Date: 2026-05-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260519_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stories",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("news_url", sa.Text(), nullable=False),
        sa.Column("news_title", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_stories_news_url", "stories", ["news_url"])
    op.create_index("ix_stories_created_at", "stories", ["created_at"])

    op.create_table(
        "claims",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "story_id",
            sa.BigInteger(),
            sa.ForeignKey("stories.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("normalized", sa.Text(), nullable=True),
        sa.Column("claim_type", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_claims_story_id", "claims", ["story_id"])

    op.create_table(
        "verdicts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "claim_id",
            sa.BigInteger(),
            sa.ForeignKey("claims.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("pipeline_version", sa.Text(), nullable=True),
        sa.Column("rules_version", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Text(), nullable=True),
        sa.Column("llm_model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_verdicts_claim_id", "verdicts", ["claim_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("queue", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("pipeline_version", sa.Text(), nullable=True),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_queue", "jobs", ["queue"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("ix_jobs_queue", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_verdicts_claim_id", table_name="verdicts")
    op.drop_table("verdicts")

    op.drop_index("ix_claims_story_id", table_name="claims")
    op.drop_table("claims")

    op.drop_index("ix_stories_created_at", table_name="stories")
    op.drop_index("ix_stories_news_url", table_name="stories")
    op.drop_table("stories")
