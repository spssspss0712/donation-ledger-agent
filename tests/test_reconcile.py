from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from donation_ledger.config import ConfigError
from donation_ledger.reconcile import StripePaymentIntentSource, run_reconcile
from donation_ledger.webhook import process_event
from tests.helpers import event, payment_intent
from tests.test_sync import rows


class FakeStripe:
    """Fake at the Stripe boundary: returns canned payment intents, records the query."""

    def __init__(self, intents: list[dict[str, Any]], fail_after: int | None = None) -> None:
        self.intents = intents
        self.asked_since: datetime | None = None
        self.fail_after = fail_after

    def list_since(self, created_gte: datetime) -> Iterable[Mapping[str, Any]]:
        self.asked_since = created_gte
        for i, pi in enumerate(self.intents):
            if self.fail_after is not None and i >= self.fail_after:
                raise ConnectionError("stripe unreachable (injected)")
            yield pi


NOW = datetime(2025, 1, 10, tzinfo=UTC)


def reconcile(engine: Engine, fake: FakeStripe, batch: int = 100):  # type: ignore[no-untyped-def]
    return run_reconcile(engine, fake, timedelta(hours=72), batch, now=NOW)


def test_missed_payments_are_added_and_the_lookback_window_is_requested(engine: Engine) -> None:
    fake = FakeStripe([payment_intent("pi_R1"), payment_intent("pi_R2", amount=700)])
    res = reconcile(engine, fake)
    assert res.status == "succeeded" and res.counters.rows_inserted == 2
    assert fake.asked_since == NOW - timedelta(hours=72)
    assert rows(engine, "SELECT source, external_id FROM donations ORDER BY external_id") == [
        ("stripe", "pi_R1"),
        ("stripe", "pi_R2"),
    ]
    assert rows(engine, "SELECT source, status FROM sync_runs") == [
        ("stripe_reconcile", "succeeded")
    ]


def test_payments_already_delivered_by_webhook_are_not_duplicated(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    process_event(session_factory, event(payment_intent("pi_BOTH")))
    res = reconcile(
        engine, FakeStripe([payment_intent("pi_BOTH"), payment_intent("pi_ONLY_RECON")])
    )
    assert res.counters.rows_inserted == 1 and res.counters.rows_duplicate_skipped == 1
    assert rows(engine, "SELECT count(*) FROM donations") == [(2,)]


def test_reconciling_twice_is_idempotent(engine: Engine) -> None:
    fake = FakeStripe([payment_intent("pi_I1")])
    reconcile(engine, fake)
    second = reconcile(engine, fake)
    assert second.counters.rows_inserted == 0
    assert rows(engine, "SELECT count(*) FROM donations") == [(1,)]


def test_payments_that_did_not_succeed_are_not_recorded(engine: Engine) -> None:
    fake = FakeStripe(
        [payment_intent("pi_P", status="processing"), payment_intent("pi_C", status="canceled")]
    )
    reconcile(engine, fake)
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]


def test_unusable_payment_is_quarantined_and_does_not_block_the_others(engine: Engine) -> None:
    fake = FakeStripe([payment_intent("pi_BAD", amount=0), payment_intent("pi_OK")])
    res = reconcile(engine, fake)
    assert res.status == "succeeded"
    assert rows(engine, "SELECT external_id FROM donations") == [("pi_OK",)]
    assert rows(engine, "SELECT external_id, source FROM rejected_rows") == [("pi_BAD", "stripe")]


def test_stripe_outage_before_any_write_marks_the_run_failed_and_writes_nothing(
    engine: Engine,
) -> None:
    res = reconcile(engine, FakeStripe([payment_intent("pi_A")], fail_after=0))
    assert res.status == "failed"
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    assert "stripe unreachable" in (res.error or "")


def test_a_rerun_after_a_failure_completes_the_missing_payments(engine: Engine) -> None:
    intents = [payment_intent(f"pi_F{i}") for i in range(5)]
    assert reconcile(engine, FakeStripe(intents, fail_after=3)).status == "failed"
    assert reconcile(engine, FakeStripe(intents)).status == "succeeded"
    assert rows(engine, "SELECT count(*) FROM donations") == [(5,)]


def test_same_payment_listed_twice_in_one_batch_is_stored_once(engine: Engine) -> None:
    reconcile(engine, FakeStripe([payment_intent("pi_D"), payment_intent("pi_D")]))
    assert rows(engine, "SELECT count(*) FROM donations") == [(1,)]


def test_live_stripe_keys_are_refused() -> None:
    with pytest.raises(ConfigError):
        StripePaymentIntentSource("sk_live_FAKE_do_not_use")


def test_reconcile_uses_a_lock_separate_from_the_sheet_sync(engine: Engine) -> None:
    from donation_ledger.runs import LOCK_KEYS

    holder = engine.connect()
    try:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEYS["sheet"]})
        assert reconcile(engine, FakeStripe([payment_intent("pi_L")])).status == "succeeded"
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEYS["sheet"]})
        holder.close()
