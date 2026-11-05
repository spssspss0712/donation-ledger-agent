"""What happens when a donation that may already exist is read again.

Used by all three paths that can see a donation twice (webhook duplicates, the reconciliation
job, the sheet sync). Policy (SPEC 2.3):

* money fields are immutable (also enforced by a database trigger): a difference only
  produces a ``changed_after_sync`` warning;
* contact fields are filled in once, while ``NULL``, by a conditional UPDATE, so concurrent
  writers cannot overwrite each other; every fill is audited (without the value);
* a non-NULL contact value is never overwritten and never blanked; a different later value
  produces a ``contact_changed_after_sync`` warning;
* a malformed email is stored as NULL and produces an ``invalid_email`` warning.

Everything here runs in the caller's transaction, so a failed batch leaves nothing behind.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from donation_ledger.ledger import NewDonation, fingerprint_of, record_issue
from donation_ledger.models import CONTACT_FIELDS, Donation, DonationContactChange

ISSUE_CHANGED_AFTER_SYNC = "changed_after_sync"
ISSUE_CONTACT_CHANGED = "contact_changed_after_sync"
ISSUE_INVALID_EMAIL = "invalid_email"
ISSUE_CONTACT_TRUNCATED = "contact_truncated"


@dataclass
class ContactOutcome:
    filled: int = 0
    issues: int = 0


def _digest(value: str) -> str:
    """Short fingerprint so a warning is recorded once per distinct value, without the value."""
    return fingerprint_of(value)[:16]


def apply_reads(
    session: Session,
    items: Sequence[NewDonation],
    inserted: set[str],
    run_id: int | None,
    *,
    check_money: bool = True,
) -> ContactOutcome:
    """Apply the policy for ``items`` (all the same source) after ``insert_donations``.

    ``inserted`` are the external IDs that this transaction just created; every other item
    is an existing donation. ``check_money=False`` is for the sheet, which compares the raw
    cell text itself (so a reformatted amount is still reported).
    """
    out = ContactOutcome()
    if not items:
        return out
    source = items[0].source
    existing = {i.external_id: i for i in items if i.external_id not in inserted}
    stored = _load(session, source, list(existing))
    # Sorted: concurrent batches update rows in the same order, so they cannot deadlock.
    for item in sorted(items, key=lambda i: i.external_id):
        if item.invalid_email is not None:
            out.issues += record_issue(
                session,
                source,
                item.external_id,
                ISSUE_INVALID_EMAIL,
                {"reason": "malformed_email", "digest": _digest(item.invalid_email)},
                run_id,
            )
        if item.truncated:
            out.issues += record_issue(
                session,
                source,
                item.external_id,
                ISSUE_CONTACT_TRUNCATED,
                {"fields": sorted(item.truncated)},
                run_id,
            )
        row = stored.get(item.external_id)
        if item.external_id in inserted or row is None:
            continue
        changed_money = _changed_money(item, row)
        if check_money and changed_money:
            out.issues += _warn_money(session, item, row, changed_money, run_id)
        if changed_money:
            # Same ID but different money: possibly a different donation that reuses the ID.
            # Attaching its contact details to this one could mislabel a donor, so do nothing.
            continue
        for field in CONTACT_FIELDS:
            _apply_field(session, item, row, field, run_id, out)
    return out


def _load(session: Session, source: str, ids: list[str]) -> dict[str, Donation]:
    if not ids:
        return {}
    stmt = select(Donation).where(Donation.source == source, Donation.external_id.in_(ids))
    # populate_existing: the identity map must not hide values another writer just committed.
    return {
        d.external_id: d for d in session.scalars(stmt.execution_options(populate_existing=True))
    }


def _changed_money(item: NewDonation, row: Donation) -> list[str]:
    return [
        f for f in ("amount_minor", "currency", "donated_at") if getattr(item, f) != getattr(row, f)
    ]


def _warn_money(
    session: Session, item: NewDonation, row: Donation, changed: list[str], run_id: int | None
) -> int:
    detail: dict[str, Any] = {
        "changed_fields": changed,
        "stored": {f: str(getattr(row, f)) for f in changed},
        "current": {f: str(getattr(item, f)) for f in changed},
    }
    return record_issue(
        session, item.source, item.external_id, ISSUE_CHANGED_AFTER_SYNC, detail, run_id
    )


def _apply_field(
    session: Session,
    item: NewDonation,
    row: Donation,
    field: str,
    run_id: int | None,
    out: ContactOutcome,
) -> None:
    new = getattr(item, field)
    if new is None:
        return  # a read without a value never clears anything
    current = getattr(row, field)
    if current is None:
        if _fill(session, row, field, new, item.source, run_id):
            out.filled += 1
            return
        # Lost the race: someone filled it between our read and our UPDATE.
        current = session.scalar(select(getattr(Donation, field)).where(Donation.id == row.id))
    if current != new:
        out.issues += record_issue(
            session,
            item.source,
            item.external_id,
            ISSUE_CONTACT_CHANGED,
            {"field": field, "digest": _digest(new)},
            run_id,
        )


def _fill(
    session: Session, row: Donation, field: str, value: str, source: str, run_id: int | None
) -> bool:
    values: dict[str, Any] = {field: value}
    if field == "donor_name":
        # Same statement, because CHECK is_anonymous = (donor_name IS NULL).
        values["is_anonymous"] = False
    stmt = (
        update(Donation)
        .where(Donation.id == row.id, getattr(Donation, field).is_(None))
        .values(**values)
        .returning(Donation.id)
        .execution_options(synchronize_session=False)
    )
    if session.execute(stmt).first() is None:
        return False
    session.add(
        DonationContactChange(
            donation_id=row.id, field_name=field, source=source, sync_run_id=run_id
        )
    )
    session.flush()
    return True
