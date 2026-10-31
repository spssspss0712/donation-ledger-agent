"""Initial schema: donations, sync_runs, rejected_rows, sync_issues.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("rows_read", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_duplicate_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_quarantined", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_ignored", sa.Integer(), server_default="0", nullable=False),
        sa.Column("issues_recorded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'failed', 'partial')", name="ck_sync_runs_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "donations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("donor_name", sa.Text(), nullable=True),
        sa.Column("donor_email", sa.Text(), nullable=True),
        sa.Column("donor_address", sa.Text(), nullable=True),
        sa.Column("recipient", sa.Text(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("donated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_anonymous", sa.Boolean(), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source IN ('stripe', 'sheet')", name="ck_donations_source"),
        sa.CheckConstraint("external_id <> ''", name="ck_donations_external_id_nonempty"),
        sa.CheckConstraint("amount_minor > 0", name="ck_donations_amount_positive"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_donations_currency_iso"),
        sa.CheckConstraint("is_anonymous = (donor_name IS NULL)", name="ck_donations_anonymous_matches_name"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id", name="uq_donations_source_external_id"),
    )
    op.create_index("ix_donations_donated_at", "donations", ["donated_at"])
    op.create_table(
        "rejected_rows",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("raw_row", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("row_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("first_seen_run_id", sa.BigInteger(), nullable=True),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('open', 'resolved', 'absent', 'ignored')", name="ck_rejected_status"),
        sa.ForeignKeyConstraint(["first_seen_run_id"], ["sync_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "row_fingerprint", name="uq_rejected_rows_source_fingerprint"),
    )
    op.create_index("ix_rejected_rows_source_external_id", "rejected_rows", ["source", "external_id"])
    op.create_table(
        "sync_issues",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("detail_hash", sa.String(length=64), nullable=False),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id", "kind", "detail_hash", name="uq_sync_issues_dedupe"),
    )


def downgrade() -> None:
    op.drop_table("sync_issues")
    op.drop_table("rejected_rows")
    op.drop_index("ix_donations_donated_at", table_name="donations")
    op.drop_table("donations")
    op.drop_table("sync_runs")
