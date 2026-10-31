"""Google Sheet -> ledger sync. Entry point: ``python -m donation_ledger.sync``.

Strategy: FULL COMPARISON on every run (no "last synced position"), so a quarantined row
can never be skipped forever and a rerun after any failure is always correct. Idempotency
comes from the (source, external_id) unique constraint, not from remembering progress.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from donation_ledger.config import ConfigError, Settings
from donation_ledger.db import make_engine
from donation_ledger.ledger import (
    NewDonation,
    Rejection,
    fingerprint_of,
    insert_donations,
    record_rejection,
)
from donation_ledger.logging_setup import configure_logging
from donation_ledger.models import SOURCE_SHEET, Donation, RejectedRow, SyncIssue, SyncRun
from donation_ledger.runs import (
    Counters,
    LockNotAcquiredError,
    add_counters,
    advisory_lock,
    assert_lock_still_held,
    fail_stale_runs,
    finish_run,
    safe_error_message,
    start_run,
)
from donation_ledger.sheet_rows import Candidate, Classified, HeaderError, Rejected, classify_table
from donation_ledger.sources import SheetReader, SheetReadError, make_reader

log = logging.getLogger(__name__)

ISSUE_CHANGED_AFTER_SYNC = "changed_after_sync"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 3


@dataclass(frozen=True)
class SyncResult:
    run_id: int | None
    status: str  # succeeded | partial | failed | locked
    counters: Counters
    error: str | None = None


def chunks[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def run_sync(
    engine: Engine,
    reader: SheetReader,
    tz_name: str,
    batch_size: int = 500,
    now: datetime | None = None,
) -> SyncResult:
    try:
        with advisory_lock(engine, SOURCE_SHEET) as lock_conn:
            return _run_locked(engine, lock_conn, reader, tz_name, batch_size, now)
    except LockNotAcquiredError:
        return _record_locked_out(engine)


def _record_locked_out(engine: Engine) -> SyncResult:
    message = "another sync is already running; this run did nothing"
    run_id: int | None = None
    try:
        with Session(engine) as session, session.begin():
            run = SyncRun(source=SOURCE_SHEET, status="failed", error_message=message)
            run.finished_at = datetime.now().astimezone()
            session.add(run)
            session.flush()
            run_id = run.id
    except Exception as exc:  # noqa: BLE001 - best effort bookkeeping
        log.error("could not record locked-out run", extra={"error": safe_error_message(exc)})
    log.warning("sync skipped: lock held by another run")
    return SyncResult(run_id, "locked", Counters(), message)


def _run_locked(
    engine: Engine,
    lock_conn: Any,
    reader: SheetReader,
    tz_name: str,
    batch_size: int,
    now: datetime | None,
) -> SyncResult:
    with Session(engine) as session, session.begin():
        stale = fail_stale_runs(session, SOURCE_SHEET)
        run_id = start_run(session, SOURCE_SHEET)
    if stale:
        log.warning("marked crashed runs as failed", extra={"count": stale})
    counters = Counters()
    committed_batches = 0
    log.info("sync started", extra={"run_id": run_id})
    try:
        table = reader.read()
        outcomes = classify_table(table, tz_name, now)
        with Session(engine) as session:
            ignored = _ignored_fingerprints(session)
        for batch in chunks(outcomes, batch_size):
            assert_lock_still_held(lock_conn)
            with Session(engine) as session, session.begin():
                batch_counters = _process_batch(session, batch, run_id, ignored)
                add_counters(session, run_id, batch_counters)
            counters.add(batch_counters)
            committed_batches += 1
        assert_lock_still_held(lock_conn)
        with Session(engine) as session, session.begin():
            _mark_absent_rejections(session, run_id)
            finish_run(session, run_id, "succeeded", None)
    except (HeaderError, SheetReadError) as exc:
        return _fail(engine, run_id, counters, "failed", f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - any failure must end up in sync_runs
        status = "partial" if committed_batches else "failed"
        return _fail(engine, run_id, counters, status, safe_error_message(exc))
    log.info("sync finished", extra={"run_id": run_id, **counters.__dict__})
    return SyncResult(run_id, "succeeded", counters)


def _fail(engine: Engine, run_id: int, counters: Counters, status: str, message: str) -> SyncResult:
    log.error("sync failed", extra={"run_id": run_id, "status": status, "error": message})
    try:
        with Session(engine) as session, session.begin():
            finish_run(session, run_id, status, message)
    except Exception as exc:  # noqa: BLE001
        # Leaves the row 'running'; the next lock holder marks it failed.
        log.error("could not record failure", extra={"error": safe_error_message(exc)})
    return SyncResult(run_id, status, counters, message)


def _ignored_fingerprints(session: Session) -> set[str]:
    stmt = select(RejectedRow.row_fingerprint).where(
        RejectedRow.source == SOURCE_SHEET, RejectedRow.status == "ignored"
    )
    return set(session.scalars(stmt))


def _process_batch(
    session: Session, batch: Sequence[Classified], run_id: int, ignored: set[str]
) -> Counters:
    c = Counters(rows_read=len(batch))
    ids = [o.entry_id for o in batch if isinstance(o, Candidate)]
    existing = _load_existing(session, ids)
    to_insert: list[NewDonation] = []
    for outcome in batch:
        if isinstance(outcome, Rejected):
            _quarantine(session, outcome.rejection, run_id, ignored, c)
            continue
        if outcome.entry_id in existing:
            c.rows_duplicate_skipped += 1
            c.issues_recorded += _warn_if_changed(
                session, outcome, existing[outcome.entry_id], run_id
            )
        elif isinstance(outcome.result, NewDonation):
            # "ignored" only silences rejections; a row that became valid must be ingested.
            to_insert.append(outcome.result)
        else:
            _quarantine(session, outcome.result, run_id, ignored, c)
    inserted = insert_donations(session, to_insert)
    c.rows_inserted += len(inserted)
    # Not inserted although new a moment ago: someone else wrote it first. Still a duplicate.
    c.rows_duplicate_skipped += len(to_insert) - len(inserted)
    return c


def _quarantine(
    session: Session, rej: Rejection, run_id: int, ignored: set[str], c: Counters
) -> None:
    if rej.fingerprint in ignored:
        c.rows_ignored += 1
        return
    record_rejection(session, rej, run_id)
    c.rows_quarantined += 1


def _load_existing(session: Session, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    stmt = select(Donation.external_id, Donation.raw_payload).where(
        Donation.source == SOURCE_SHEET, Donation.external_id.in_(ids)
    )
    return {ext: payload for ext, payload in session.execute(stmt)}


def _warn_if_changed(session: Session, row: Candidate, stored: dict[str, Any], run_id: int) -> int:
    """Append-only ledger: never update the donation, only record a (de-duplicated) warning."""
    old = stored.get("fields") if isinstance(stored, dict) else None
    old = old if isinstance(old, dict) else {}
    changed = sorted(k for k, v in row.fields.items() if old.get(k) != v)
    if not changed:
        return 0
    detail = {
        "changed_fields": changed,
        "stored": {k: old.get(k) for k in changed},
        "current": {k: row.fields[k] for k in changed},
    }
    stmt = (
        pg_insert(SyncIssue)
        .values(
            source=SOURCE_SHEET,
            external_id=row.entry_id,
            kind=ISSUE_CHANGED_AFTER_SYNC,
            detail=detail,
            detail_hash=fingerprint_of(detail),
            sync_run_id=run_id,
        )
        .on_conflict_do_nothing(constraint="uq_sync_issues_dedupe")
        .returning(SyncIssue.id)
    )
    return 1 if session.execute(stmt).first() is not None else 0


def _mark_absent_rejections(session: Session, run_id: int) -> None:
    """Open rejections not seen in a fully successful run are no longer in the sheet.

    They become 'absent' (not 'resolved'): nothing was fixed and no donation exists, and
    they reopen automatically if the same row comes back.
    """
    session.execute(
        update(RejectedRow)
        .where(
            RejectedRow.source == SOURCE_SHEET,
            RejectedRow.status == "open",
            RejectedRow.sync_run_id.is_distinct_from(run_id),
        )
        .values(status="absent")
    )


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    try:
        settings = Settings.from_env()
        reader = make_reader(settings)
    except (ConfigError, SheetReadError) as exc:
        log.error("configuration error", extra={"error": str(exc)})
        return EXIT_FAILED
    engine = make_engine(settings.database_url)
    try:
        result = run_sync(engine, reader, settings.sheet_timezone, settings.sync_batch_size)
    finally:
        engine.dispose()
    if result.status == "succeeded":
        return EXIT_OK
    return EXIT_LOCKED if result.status == "locked" else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
