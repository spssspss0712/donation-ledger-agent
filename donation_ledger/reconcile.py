"""Stripe reconciliation: add payments the webhook missed. ``python -m donation_ledger.reconcile``.

Safe to run at any time and concurrently with live webhooks: every insert goes through the
same (source, external_id) unique constraint, so a payment is stored once no matter which
path delivers it first.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from donation_ledger.config import ConfigError, Settings
from donation_ledger.contacts import apply_reads
from donation_ledger.db import make_engine
from donation_ledger.ledger import (
    NewDonation,
    Rejection,
    insert_donations,
    lock_external_ids,
    record_rejection,
)
from donation_ledger.logging_setup import configure_logging
from donation_ledger.models import RUN_STRIPE_RECONCILE, SOURCE_STRIPE, RejectedRow
from donation_ledger.runs import (
    Counters,
    LockNotAcquiredError,
    add_counters,
    advisory_lock,
    assert_lock_still_held,
    assert_run_still_ours,
    fail_stale_runs,
    finish_run,
    safe_error_message,
    start_run,
)
from donation_ledger.stripe_mapping import map_payment_intent
from donation_ledger.sync import EXIT_FAILED, EXIT_LOCKED, EXIT_OK, SyncResult, chunks
from donation_ledger.webhook import donation_exists

log = logging.getLogger(__name__)


class PaymentIntentSource(Protocol):
    """Anything that can list recent payment intents. Tests use a fake; prod uses Stripe."""

    def list_since(self, created_gte: datetime) -> Iterable[Mapping[str, Any]]: ...


class StripePaymentIntentSource:
    """Real Stripe-backed source.

    UNVERIFIED: written against the stripe-python 16.x docs, never run against Stripe
    (no test-mode key was available). Verify in Phase B.
    """

    def __init__(self, api_key: str) -> None:
        if not api_key.startswith(("sk_test_", "rk_test_")):
            raise ConfigError("only Stripe test-mode keys are accepted")
        import stripe

        self._client = stripe.StripeClient(api_key)

    def list_since(self, created_gte: datetime) -> Iterable[Mapping[str, Any]]:
        params: Any = {
            "created": {"gte": int(created_gte.timestamp())},
            "limit": 100,
        }
        for pi in self._client.payment_intents.list(params=params).auto_paging_iter():
            yield pi.to_dict()


def run_reconcile(
    engine: Engine,
    source: PaymentIntentSource,
    lookback: timedelta,
    batch_size: int = 100,
    now: datetime | None = None,
) -> SyncResult:
    try:
        with advisory_lock(engine, RUN_STRIPE_RECONCILE) as lock_conn:
            return _run_locked(engine, lock_conn, source, lookback, batch_size, now)
    except LockNotAcquiredError:
        log.warning("reconcile skipped: lock held by another run")
        return SyncResult(None, "locked", Counters(), "another reconcile is running")


def _run_locked(
    engine: Engine,
    lock_conn: Any,
    source: PaymentIntentSource,
    lookback: timedelta,
    batch_size: int,
    now: datetime | None,
) -> SyncResult:
    with Session(engine) as session, session.begin():
        fail_stale_runs(session, RUN_STRIPE_RECONCILE)
        run_id = start_run(session, RUN_STRIPE_RECONCILE)
    counters = Counters()
    committed = 0
    try:
        since = (now or datetime.now(UTC)) - lookback
        # Materialise first: a broken Stripe listing must fail before we write anything.
        intents = [pi for pi in source.list_since(since) if pi.get("status") == "succeeded"]
        # Payments quarantined earlier under a stricter rule (e.g. the old currency whitelist)
        # are older than the lookback window, so listing would never see them again.
        for redo in chunks(_open_stripe_rejections(engine), batch_size):
            assert_lock_still_held(lock_conn)
            with Session(engine) as session, session.begin():
                redo_counters = _redrive(session, redo, run_id)
                add_counters(session, run_id, redo_counters)
            counters.add(redo_counters)
            committed += 1
        for batch in chunks(intents, batch_size):
            assert_lock_still_held(lock_conn)
            with Session(engine) as session, session.begin():
                batch_counters = _process(session, batch, run_id)
                add_counters(session, run_id, batch_counters)
            counters.add(batch_counters)
            committed += 1
        with Session(engine) as session, session.begin():
            assert_run_still_ours(session, run_id)
            finish_run(session, run_id, "succeeded", None)
    except Exception as exc:  # noqa: BLE001
        status = "partial" if committed else "failed"
        message = safe_error_message(exc)
        log.error("reconcile failed", extra={"run_id": run_id, "error": message})
        try:
            with Session(engine) as session, session.begin():
                finish_run(session, run_id, status, message)
        except Exception as inner:  # noqa: BLE001
            log.error("could not record failure", extra={"error": safe_error_message(inner)})
        return SyncResult(run_id, status, counters, message)
    log.info("reconcile finished", extra={"run_id": run_id, **counters.__dict__})
    return SyncResult(run_id, "succeeded", counters)


def _open_stripe_rejections(engine: Engine) -> list[dict[str, Any]]:
    stmt = (
        select(RejectedRow.raw_row)
        .where(
            RejectedRow.source == SOURCE_STRIPE,
            RejectedRow.status == "open",
            RejectedRow.external_id.is_not(None),
        )
        .order_by(RejectedRow.id)
    )
    with Session(engine) as session:
        return list(session.scalars(stmt))


def _redrive(session: Session, stored: Sequence[Mapping[str, Any]], run_id: int) -> Counters:
    """Insert the quarantined payments that map cleanly under the CURRENT rules.

    Those that still fail are left exactly as they are (no attempt counter, no new row).
    Inserting resolves the matching open rejection (``insert_donations``).
    """
    c = Counters(rows_read=len(stored))
    ready = [m for m in (map_payment_intent(raw) for raw in stored) if isinstance(m, NewDonation)]
    lock_external_ids(session, SOURCE_STRIPE, [d.external_id for d in ready])
    inserted = insert_donations(session, ready)
    outcome = apply_reads(session, ready, inserted, run_id)
    c.rows_inserted += len(inserted)
    c.rows_duplicate_skipped += len(ready) - len(inserted)
    c.contacts_filled += outcome.filled
    c.issues_recorded += outcome.issues
    return c


def _process(session: Session, batch: Sequence[Mapping[str, Any]], run_id: int) -> Counters:
    c = Counters(rows_read=len(batch))
    donations: list[NewDonation] = []
    seen: set[str] = set()
    mapped_all = [map_payment_intent(pi) for pi in batch]
    lock_external_ids(session, SOURCE_STRIPE, [m.external_id for m in mapped_all])
    for mapped in mapped_all:
        if isinstance(mapped, Rejection):
            if mapped.external_id and donation_exists(session, mapped.external_id):
                c.rows_duplicate_skipped += 1  # stale bad snapshot of a payment we hold
            elif record_rejection(session, mapped, run_id) == "ignored":
                c.rows_ignored += 1
            else:
                c.rows_quarantined += 1
        elif mapped.external_id in seen:
            c.rows_duplicate_skipped += 1  # same PI listed twice in one batch
        else:
            seen.add(mapped.external_id)
            donations.append(mapped)
    inserted = insert_donations(session, donations)
    outcome = apply_reads(session, donations, inserted, run_id)
    c.contacts_filled += outcome.filled
    c.issues_recorded += outcome.issues
    c.rows_inserted += len(inserted)
    c.rows_duplicate_skipped += len(donations) - len(inserted)
    return c


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    try:
        settings = Settings.from_env()
        if not settings.stripe_api_key:
            raise ConfigError("STRIPE_API_KEY is required for reconciliation")
        source = StripePaymentIntentSource(settings.stripe_api_key)
    except ConfigError as exc:
        log.error("configuration error", extra={"error": str(exc)})
        return EXIT_FAILED
    engine = make_engine(settings.database_url)
    try:
        result = run_reconcile(engine, source, timedelta(hours=settings.reconcile_lookback_hours))
    except Exception as exc:  # noqa: BLE001 - e.g. database unreachable at startup
        log.error("reconcile could not start", extra={"error": safe_error_message(exc)})
        return EXIT_FAILED
    finally:
        engine.dispose()
    if result.status == "succeeded":
        return EXIT_OK
    return EXIT_LOCKED if result.status == "locked" else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
