"""SQLAlchemy models. The Alembic migration must stay identical (a test checks it)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SOURCE_STRIPE = "stripe"
SOURCE_SHEET = "sheet"
RUN_STRIPE_RECONCILE = "stripe_reconcile"


class Base(DeclarativeBase):
    pass


class Donation(Base):
    __tablename__ = "donations"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_donations_source_external_id"),
        CheckConstraint("source IN ('stripe', 'sheet')", name="ck_donations_source"),
        CheckConstraint("external_id <> ''", name="ck_donations_external_id_nonempty"),
        CheckConstraint("amount_minor > 0", name="ck_donations_amount_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_donations_currency_iso"),
        # "Anonymous" is a presentation concern: anonymous <=> NULL name.
        CheckConstraint(
            "is_anonymous = (donor_name IS NULL)", name="ck_donations_anonymous_matches_name"
        ),
        Index("ix_donations_donated_at", "donated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(255))
    donor_name: Mapped[str | None] = mapped_column(Text)
    donor_email: Mapped[str | None] = mapped_column(Text)
    donor_address: Mapped[str | None] = mapped_column(Text)
    recipient: Mapped[str | None] = mapped_column(Text)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    donated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_anonymous: Mapped[bool] = mapped_column(Boolean)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'partial')", name="ck_sync_runs_status"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), server_default="running")
    rows_read: Mapped[int] = mapped_column(Integer, server_default="0")
    rows_inserted: Mapped[int] = mapped_column(Integer, server_default="0")
    rows_duplicate_skipped: Mapped[int] = mapped_column(Integer, server_default="0")
    rows_quarantined: Mapped[int] = mapped_column(Integer, server_default="0")
    rows_ignored: Mapped[int] = mapped_column(Integer, server_default="0")
    issues_recorded: Mapped[int] = mapped_column(Integer, server_default="0")
    contacts_filled: Mapped[int] = mapped_column(Integer, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text)


class RejectedRow(Base):
    __tablename__ = "rejected_rows"
    __table_args__ = (
        UniqueConstraint("source", "row_fingerprint", name="uq_rejected_rows_source_fingerprint"),
        CheckConstraint(
            "status IN ('open', 'resolved', 'absent', 'ignored')", name="ck_rejected_status"
        ),
        Index("ix_rejected_rows_source_external_id", "source", "external_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str | None] = mapped_column(String(255))
    raw_row: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text)
    row_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    attempts: Mapped[int] = mapped_column(Integer, server_default="1")
    first_seen_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    sync_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SyncIssue(Base):
    __tablename__ = "sync_issues"
    __table_args__ = (
        UniqueConstraint(
            "source", "external_id", "kind", "detail_hash", name="uq_sync_issues_dedupe"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)
    detail_hash: Mapped[str] = mapped_column(String(64))
    sync_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


CONTACT_FIELDS = ("donor_name", "donor_email", "donor_address", "recipient")
# Immutable in the database by trigger (migration 0002) and never written by application code.
MONEY_FIELDS = ("amount_minor", "currency", "donated_at", "source", "external_id")


class DonationContactChange(Base):
    """Audit of fill-once contact updates. The new value is deliberately NOT stored here."""

    __tablename__ = "donation_contact_changes"
    __table_args__ = (
        CheckConstraint(
            "field_name IN ('donor_name', 'donor_email', 'donor_address', 'recipient')",
            name="ck_contact_changes_field",
        ),
        Index("ix_contact_changes_donation_id", "donation_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    donation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("donations.id"))
    field_name: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16))
    sync_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
