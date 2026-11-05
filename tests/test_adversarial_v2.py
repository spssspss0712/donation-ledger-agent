"""Version 2 attacks on the contact rules: crashes mid-fill, concurrency, PII in logs."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from donation_ledger.logging_setup import JsonFormatter
from donation_ledger.reconcile import run_reconcile
from donation_ledger.webhook import process_event
from tests.conftest import database_url_for_tests
from tests.helpers import event as stripe_event
from tests.helpers import payment_intent
from tests.test_reconcile import NOW as RECON_NOW  # noqa: F401
from tests.test_reconcile import FakeStripe
from tests.test_sync import HEADER, ListReader, rows, sync
from tests.test_sync_contacts import row


def test_connection_dropped_while_filling_contacts_rolls_the_batch_back_and_rerun_heals(
    engine: Engine,
) -> None:
    n = 12
    base = [row(f"K{i:02d}", amount=str(i + 1)) for i in range(n)]
    filled = [
        row(f"K{i:02d}", name=f"Donor {i}", email=f"d{i}@b.org", amount=str(i + 1))
        for i in range(n)
    ]
    sync(engine, ListReader(base), batch_size=4)
    seen = {"n": 0}

    def kill(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("INSERT INTO DONATION_CONTACT_CHANGES"):
            seen["n"] += 1
            if seen["n"] == 5:  # inside the second batch (each batch makes 8 fills)
                cursor.execute("SELECT pg_terminate_backend(pg_backend_pid())")

    event.listen(engine, "before_cursor_execute", kill)
    try:
        res = sync(engine, ListReader(filled), batch_size=4)
    finally:
        event.remove(engine, "before_cursor_execute", kill)
    assert res.status in ("partial", "failed")
    # Only whole batches survive: fills and audit rows always match one to one.
    fills = int(
        str(rows(engine, "SELECT count(*) FROM donations WHERE donor_email IS NOT NULL")[0][0])
    )
    audits = int(
        str(
            rows(
                engine,
                "SELECT count(*) FROM donation_contact_changes WHERE field_name='donor_email'",
            )[0][0]
        )
    )
    assert fills == audits and fills % 4 == 0 and fills < n
    assert sync(engine, ListReader(filled), batch_size=4).status == "succeeded"
    assert rows(engine, "SELECT count(*) FROM donations WHERE donor_email IS NOT NULL") == [(n,)]
    assert rows(engine, "SELECT count(*) FROM donation_contact_changes") == [(2 * n,)]


def test_process_killed_with_sigkill_right_after_a_fill_leaves_no_half_fill(
    engine: Engine, tmp_path: Path
) -> None:
    sync(engine, ListReader([row("Z1"), row("Z2")]))
    data = tmp_path / "sheet.csv"
    data.write_text(
        ",".join(HEADER)
        + "\nZ1,2024-03-01,Zed,z1@b.org,,10,USD,\nZ2,2024-03-01,Zoe,z2@b.org,,10,USD,\n",
        encoding="utf-8",
    )
    script = tmp_path / "crash.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os
            from datetime import UTC, datetime
            from sqlalchemy import event
            from donation_ledger.db import make_engine
            from donation_ledger.sources import CsvSheetReader
            from donation_ledger.sync import run_sync

            engine = make_engine({database_url_for_tests()!r})

            @event.listens_for(engine, "after_cursor_execute")
            def die(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("INSERT INTO DONATION_CONTACT_CHANGES"):
                    os._exit(137)  # the UPDATE ran, the transaction was never committed

            run_sync(engine, CsvSheetReader({str(data)!r}), "America/Los_Angeles", 500,
                     now=datetime(2025, 6, 1, tzinfo=UTC))
            """
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(Path.cwd())}
    proc = subprocess.run([sys.executable, str(script)], env=env, capture_output=True)  # noqa: S603
    assert proc.returncode == 137, proc.stderr.decode()[-500:]
    assert rows(
        engine,
        "SELECT count(*) FROM donations WHERE donor_email IS NOT NULL OR donor_name IS NOT NULL",
    ) == [(0,)]
    assert rows(engine, "SELECT count(*) FROM donation_contact_changes") == [(0,)]
    res = sync(
        engine,
        ListReader(
            [row("Z1", name="Zed", email="z1@b.org"), row("Z2", name="Zoe", email="z2@b.org")]
        ),
    )
    assert res.status == "succeeded" and res.counters.contacts_filled == 4


def test_many_concurrent_webhook_deliveries_with_different_emails_keep_one_and_audit_one(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    process_event(session_factory, stripe_event(payment_intent("pi_RACE")))
    barrier = threading.Barrier(10)
    errors: list[BaseException] = []

    def deliver(n: int) -> None:
        try:
            barrier.wait()
            process_event(
                session_factory,
                stripe_event(payment_intent("pi_RACE", receipt_email=f"w{n}@b.org"), f"evt_{n}"),
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=deliver, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert rows(engine, "SELECT count(*) FROM donations") == [(1,)]
    assert rows(engine, "SELECT count(*) FROM donation_contact_changes") == [(1,)]
    assert rows(
        engine, "SELECT count(*) FROM sync_issues WHERE kind='contact_changed_after_sync'"
    ) == [(9,)]


def test_webhook_and_reconciliation_racing_to_fill_one_payment_fill_it_exactly_once(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    from datetime import timedelta

    process_event(session_factory, stripe_event(payment_intent("pi_BOTH")))
    barrier = threading.Barrier(2)

    def webhook() -> None:
        barrier.wait()
        process_event(
            session_factory, stripe_event(payment_intent("pi_BOTH", receipt_email="same@b.org"))
        )

    def reconcile() -> None:
        barrier.wait()
        run_reconcile(
            engine,
            FakeStripe([payment_intent("pi_BOTH", receipt_email="same@b.org")]),
            timedelta(hours=72),
            now=RECON_NOW,
        )

    threads = [threading.Thread(target=webhook), threading.Thread(target=reconcile)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert rows(engine, "SELECT donor_email FROM donations") == [("same@b.org",)]
    assert rows(engine, "SELECT count(*) FROM donation_contact_changes") == [(1,)]
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(0,)]


def test_logs_never_contain_emails_names_or_addresses_on_the_new_contact_paths(
    engine: Engine, session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    process_event(session_factory, stripe_event(payment_intent("pi_LOG")))
    process_event(
        session_factory,
        stripe_event(
            payment_intent(
                "pi_LOG",
                receipt_email="leaky.stripe@example.org",
                metadata={"donor_name": "Leaky Stripe"},
            ),
            "evt_2",
        ),
    )
    process_event(
        session_factory, stripe_event(payment_intent("pi_LOG2", receipt_email="bad email"))
    )
    sync(engine, ListReader([row("L1")]))
    sync(
        engine,
        ListReader(
            [
                row(
                    "L1",
                    name="Leaky Sheet",
                    email="leaky.sheet@example.org",
                    address="1 Leaky Road",
                ),
                row("L2", email="broken leaky"),
            ]
        ),
    )
    logged = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert logged
    for secret in (
        "leaky.stripe@example.org",
        "Leaky Stripe",
        "leaky.sheet@example.org",
        "Leaky Sheet",
        "1 Leaky Road",
        "broken leaky",
        "bad email",
    ):
        assert secret not in logged


def test_sync_error_message_stored_for_a_failed_fill_contains_no_contact_values(
    engine: Engine,
) -> None:
    sync(engine, ListReader([row("M1")]))

    def boom(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("INSERT INTO DONATION_CONTACT_CHANGES"):
            cursor.execute("SELECT 1/0")

    event.listen(engine, "before_cursor_execute", boom)
    try:
        res = sync(engine, ListReader([row("M1", name="Secret Name", email="secret@b.org")]))
    finally:
        event.remove(engine, "before_cursor_execute", boom)
    assert res.status == "failed"
    stored = rows(engine, "SELECT error_message FROM sync_runs ORDER BY id DESC LIMIT 1")[0][0]
    assert "secret@b.org" not in str(stored) and "Secret Name" not in str(stored)


def test_tampering_with_money_through_a_second_database_session_is_rejected_even_mid_sync(
    engine: Engine,
) -> None:
    sync(engine, ListReader([row("T1", amount="10")]))
    with pytest.raises(Exception, match="immutable"), engine.begin() as conn:
        conn.execute(text("UPDATE donations SET amount_minor = amount_minor * 100"))
    assert rows(engine, "SELECT amount_minor FROM donations") == [(1000,)]
