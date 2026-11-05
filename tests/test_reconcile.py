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


def test_reconciliation_fills_an_empty_email_once_and_audits_it_with_the_run_id(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    process_event(session_factory, event(payment_intent("pi_FILL")))  # webhook without contact
    res = reconcile(engine, FakeStripe([payment_intent("pi_FILL", receipt_email="R@X.org")]))
    assert res.status == "succeeded" and res.counters.contacts_filled == 1
    assert rows(engine, "SELECT donor_email FROM donations") == [("r@x.org",)]
    run_id = rows(engine, "SELECT id FROM sync_runs")[0][0]
    assert rows(engine, "SELECT field_name, source, sync_run_id FROM donation_contact_changes") == [
        ("donor_email", "stripe", run_id)
    ]
    assert rows(engine, "SELECT contacts_filled FROM sync_runs") == [(1,)]
    # A second reconcile finds nothing more to do.
    again = reconcile(engine, FakeStripe([payment_intent("pi_FILL", receipt_email="R@X.org")]))
    assert again.counters.contacts_filled == 0
    assert rows(engine, "SELECT count(*) FROM donation_contact_changes") == [(1,)]


def test_reconciliation_never_overwrites_a_webhook_email_and_warns_about_the_difference(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    process_event(session_factory, event(payment_intent("pi_KEEP", receipt_email="a@x.org")))
    reconcile(engine, FakeStripe([payment_intent("pi_KEEP", receipt_email="b@x.org")]))
    assert rows(engine, "SELECT donor_email FROM donations") == [("a@x.org",)]
    assert rows(engine, "SELECT kind FROM sync_issues") == [("contact_changed_after_sync",)]


def test_reconciliation_never_changes_the_amount_but_still_fills_an_empty_email(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    process_event(session_factory, event(payment_intent("pi_MONEY", amount=500)))
    res = reconcile(
        engine, FakeStripe([payment_intent("pi_MONEY", amount=900, receipt_email="b@x.org")])
    )
    assert rows(engine, "SELECT donor_email, amount_minor FROM donations") == [("b@x.org", 500)]
    assert rows(engine, "SELECT kind FROM sync_issues") == [("changed_after_sync",)]
    assert res.counters.contacts_filled == 1


def test_reconciliation_batch_with_a_failure_after_a_fill_rolls_the_fill_back(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    from sqlalchemy import event as sa_event

    process_event(session_factory, event(payment_intent("pi_ATOMIC")))

    def boom(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("INSERT INTO DONATION_CONTACT_CHANGES"):
            raise RuntimeError("injected")

    sa_event.listen(engine, "before_cursor_execute", boom)
    try:
        res = reconcile(
            engine,
            FakeStripe(
                [payment_intent("pi_NEW1"), payment_intent("pi_ATOMIC", receipt_email="x@y.org")]
            ),
        )
    finally:
        sa_event.remove(engine, "before_cursor_execute", boom)
    assert res.status == "failed"
    # pi_NEW1 was in the same batch as the failed fill, so it must not exist either.
    assert rows(engine, "SELECT external_id, donor_email FROM donations") == [("pi_ATOMIC", None)]
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(0,)]


def test_reconciliation_stores_a_payment_whose_currency_is_not_in_the_iso_list(
    engine: Engine,
) -> None:
    res = reconcile(engine, FakeStripe([payment_intent("pi_ODD2", currency="qqq")]))
    assert res.counters.rows_inserted == 1 and res.counters.rows_quarantined == 0
    assert rows(engine, "SELECT currency FROM donations") == [("QQQ",)]


# --- re-driving Stripe payments quarantined by an older rule (e.g. the v1 currency whitelist) ---


def quarantine_like_v1(engine: Engine, pi: dict[str, Any], reason: str) -> None:
    import json

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rejected_rows (source, external_id, raw_row, reason, row_fingerprint)"
                " VALUES ('stripe', :e, CAST(:r AS jsonb), :why, :fp)"
            ),
            {"e": pi["id"], "r": json.dumps(pi), "why": reason, "fp": "fp-" + pi["id"]},
        )


def test_reconciliation_redrives_an_old_stripe_quarantine_that_is_now_acceptable(
    engine: Engine,
) -> None:
    old = payment_intent(
        "pi_OLDXYZ", currency="xyz", created=1_700_000_000, receipt_email="o@x.org"
    )
    quarantine_like_v1(engine, old, "invalid_currency: missing or not a 3-letter code")
    # Stripe lists nothing in the lookback window: the payment is far older than 72 hours.
    res = reconcile(engine, FakeStripe([]))
    assert res.status == "succeeded" and res.counters.rows_inserted == 1
    assert rows(engine, "SELECT external_id, currency, donor_email FROM donations") == [
        ("pi_OLDXYZ", "XYZ", "o@x.org")
    ]
    assert rows(engine, "SELECT status FROM rejected_rows") == [("resolved",)]
    again = reconcile(engine, FakeStripe([]))
    assert again.counters.rows_inserted == 0
    assert rows(engine, "SELECT count(*) FROM donations") == [(1,)]


def test_redrive_leaves_still_unacceptable_stripe_quarantines_open_and_untouched(
    engine: Engine,
) -> None:
    bad = payment_intent("pi_STILLBAD", amount=0)
    bad["amount_received"] = 0
    quarantine_like_v1(engine, bad, "invalid_amount: missing, zero or negative")
    res = reconcile(engine, FakeStripe([]))
    assert res.counters.rows_inserted == 0
    assert rows(engine, "SELECT status, attempts FROM rejected_rows") == [("open", 1)]
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]


def test_redrive_never_touches_ignored_or_sheet_rejections(engine: Engine) -> None:
    ignored = payment_intent("pi_IGN", currency="xyz")
    quarantine_like_v1(engine, ignored, "invalid_currency")
    with engine.begin() as conn:
        conn.execute(text("UPDATE rejected_rows SET status='ignored'"))
        conn.execute(
            text(
                "INSERT INTO rejected_rows (source, external_id, raw_row, reason, row_fingerprint)"
                " VALUES ('sheet', 'S-1', '{}', 'invalid_amount', 'fp-sheet')"
            )
        )
    reconcile(engine, FakeStripe([]))
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    assert sorted(r[0] for r in rows(engine, "SELECT status FROM rejected_rows")) == [  # type: ignore[type-var]
        "ignored",
        "open",
    ]


def test_redrive_resolves_a_stale_open_quarantine_whose_donation_already_exists_without_a_warning(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    process_event(session_factory, event(payment_intent("pi_BOTHWAYS", currency="eur")))
    stale = payment_intent("pi_BOTHWAYS", currency="xyz")  # an older snapshot of the same payment
    quarantine_like_v1(engine, stale, "invalid_currency: missing or not a 3-letter code")
    res = reconcile(engine, FakeStripe([]))
    assert res.status == "succeeded" and res.counters.rows_inserted == 0
    assert rows(engine, "SELECT status FROM rejected_rows") == [("resolved",)]
    assert rows(engine, "SELECT currency FROM donations") == [("EUR",)]
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(0,)]
    again = reconcile(engine, FakeStripe([]))
    assert again.counters.rows_read == 0  # nothing is open any more, nothing is re-read


def _holds_payment_lock(engine: Engine, external_id: str) -> Any:
    """Hold the per-payment advisory lock in another transaction, like a webhook would."""
    conn = engine.connect()
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"stripe:{external_id}"},
    )
    return conn


def _blocked_until_released(engine: Engine, run: Any, holder: Any) -> None:
    import threading
    import time

    done = threading.Event()

    def target() -> None:
        run()
        done.set()

    thread = threading.Thread(target=target)
    thread.start()
    time.sleep(1.0)
    still_waiting = not done.is_set()
    holder.rollback()  # releases the advisory lock
    holder.close()
    thread.join(timeout=20)
    assert still_waiting, "the job did not wait for the per-payment lock"
    assert done.is_set()


def test_reconciliation_waits_for_the_per_payment_lock_before_writing_a_listed_payment(
    engine: Engine,
) -> None:
    holder = _holds_payment_lock(engine, "pi_LOCKED1")
    _blocked_until_released(
        engine, lambda: reconcile(engine, FakeStripe([payment_intent("pi_LOCKED1")])), holder
    )
    assert rows(engine, "SELECT external_id FROM donations") == [("pi_LOCKED1",)]


def test_redrive_waits_for_the_per_payment_lock_before_writing_a_quarantined_payment(
    engine: Engine,
) -> None:
    quarantine_like_v1(engine, payment_intent("pi_LOCKED2", currency="xyz"), "invalid_currency")
    holder = _holds_payment_lock(engine, "pi_LOCKED2")
    _blocked_until_released(engine, lambda: reconcile(engine, FakeStripe([])), holder)
    assert rows(engine, "SELECT external_id FROM donations") == [("pi_LOCKED2",)]
