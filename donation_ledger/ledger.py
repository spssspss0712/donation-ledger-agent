"""Writes to the ledger. Every insert relies on the (source, external_id) unique constraint."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from donation_ledger.models import Donation, RejectedRow


@dataclass(frozen=True)
class NewDonation:
    source: str
    external_id: str
    donor_name: str | None
    donor_email: str | None
    donor_address: str | None
    recipient: str | None
    amount_minor: int
    currency: str
    donated_at: datetime
    raw_payload: dict[str, Any]
    # Set when a non-blank email was given but is malformed (stored as NULL; a warning is
    # recorded). Not a database column: the original text stays in raw_payload.
    invalid_email: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "external_id": self.external_id,
            "donor_name": self.donor_name,
            "donor_email": self.donor_email,
            "donor_address": self.donor_address,
            "recipient": self.recipient,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "donated_at": self.donated_at,
            "is_anonymous": self.donor_name is None,
            "raw_payload": self.raw_payload,
        }


@dataclass(frozen=True)
class Rejection:
    source: str
    external_id: str | None
    raw_row: dict[str, Any]
    reason: str
    fingerprint: str


def fingerprint_of(value: Any) -> str:
    """Stable identity of a rejected input (independent of row position)."""
    blob = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


def lock_external_ids(session: Session, source: str, ids: Iterable[str | None]) -> None:
    """Serialise writers of the same payment (transaction-scoped advisory locks).

    Without it a "bad snapshot" rejection and the good insert of one payment can interleave
    and leave an open rejection next to its donation. Always taken in sorted order, and a
    webhook holds only one, so there is no lock cycle.
    """
    for ext in sorted({i for i in ids if i}):
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"{source}:{ext}"},
        )


def insert_donations(session: Session, items: Sequence[NewDonation]) -> set[str]:
    """Insert, silently skipping existing rows. Returns the external IDs actually inserted.

    ON CONFLICT DO NOTHING is evaluated by PostgreSQL under the unique index, so two
    concurrent writers of the same payment can never both insert it.
    """
    if not items:
        return set()
    stmt = (
        pg_insert(Donation)
        .values([i.as_row() for i in items])
        .on_conflict_do_nothing(index_elements=["source", "external_id"])
        .returning(Donation.external_id)
    )
    inserted = set(session.execute(stmt).scalars())
    resolve_rejections_for(session, items[0].source, inserted)
    return inserted


def resolve_rejections_for(session: Session, source: str, external_ids: set[str]) -> None:
    """A donation now exists for these IDs, so earlier quarantined attempts are obsolete."""
    if not external_ids:
        return
    session.execute(
        update(RejectedRow)
        .where(
            RejectedRow.source == source,
            RejectedRow.external_id.in_(external_ids),
            RejectedRow.status == "open",
        )
        .values(status="resolved")
    )


def record_rejection(session: Session, rej: Rejection, run_id: int | None) -> str:
    """Upsert a quarantined row; returns the row's status after the upsert.

    resolved -> reopened (the problem is back); ignored stays ignored (a human decided).
    """
    base = pg_insert(RejectedRow).values(
        source=rej.source,
        external_id=rej.external_id,
        raw_row=rej.raw_row,
        reason=rej.reason,
        row_fingerprint=rej.fingerprint,
        status="open",
        first_seen_run_id=run_id,
        sync_run_id=run_id,
    )
    stmt = base.on_conflict_do_update(
        constraint="uq_rejected_rows_source_fingerprint",
        set_={
            "reason": base.excluded.reason,
            "raw_row": base.excluded.raw_row,
            "external_id": base.excluded.external_id,
            "sync_run_id": run_id,
            "attempts": RejectedRow.attempts + 1,
            "last_seen_at": func.now(),
            "status": _reopen_unless_ignored(),
        },
    ).returning(RejectedRow.status)
    return str(session.execute(stmt).scalar_one())


def _reopen_unless_ignored() -> Any:
    return case((RejectedRow.status == "ignored", "ignored"), else_="open")
