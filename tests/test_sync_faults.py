"""Fault injection against a real PostgreSQL: errors, dropped connections, killed processes."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, event, text

from donation_ledger.config import Settings
from donation_ledger.db import make_engine
from donation_ledger.runs import LOCK_KEYS
from donation_ledger.sources import SheetTable
from donation_ledger.sync import SyncResult, run_sync
from tests.conftest import database_url_for_tests
from tests.test_sync import HEADER, TZ, ListReader, good, rows

NOW = datetime(2025, 6, 1, tzinfo=UTC)


def make_rows(n: int) -> list[list[str]]:
    return [good(f"X{i:03d}", f"{i + 1}.00") for i in range(n)]


def sync(engine: Engine, reader: object, batch: int = 10) -> SyncResult:
    return run_sync(engine, reader, TZ, batch, now=NOW)  # type: ignore[arg-type]


def assert_complete_and_unduplicated(engine: Engine, n: int) -> None:
    ids = [r[0] for r in rows(engine, "SELECT external_id FROM donations ORDER BY external_id")]
    assert ids == [f"X{i:03d}" for i in range(n)]
    total = rows(engine, "SELECT sum(amount_minor) FROM donations")[0][0]
    assert total == sum((i + 1) * 100 for i in range(n))


def test_error_raised_midway_rolls_back_only_the_failing_batch_and_rerun_completes(
    engine: Engine,
) -> None:
    n = 35
    calls = {"inserts": 0}

    def boom(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("INSERT INTO DONATIONS"):
            calls["inserts"] += 1
            if calls["inserts"] == 3:
                raise RuntimeError("injected failure on the third batch")

    event.listen(engine, "before_cursor_execute", boom)
    try:
        first = sync(engine, ListReader(make_rows(n)))
    finally:
        event.remove(engine, "before_cursor_execute", boom)
    assert first.status == "partial"
    assert "injected failure" in (first.error or "")
    # Exactly two whole batches committed; the failing batch left nothing behind.
    assert rows(engine, "SELECT count(*) FROM donations") == [(20,)]
    assert rows(engine, "SELECT status, rows_inserted FROM sync_runs") == [("partial", 20)]
    second = sync(engine, ListReader(make_rows(n)))
    assert second.status == "succeeded"
    assert second.counters.rows_inserted == 15 and second.counters.rows_duplicate_skipped == 20
    assert_complete_and_unduplicated(engine, n)


def test_failure_in_the_very_first_batch_is_reported_failed_not_partial(engine: Engine) -> None:
    def boom(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("INSERT INTO DONATIONS"):
            raise RuntimeError("injected")

    event.listen(engine, "before_cursor_execute", boom)
    try:
        res = sync(engine, ListReader(make_rows(5)))
    finally:
        event.remove(engine, "before_cursor_execute", boom)
    assert res.status == "failed"
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    assert sync(engine, ListReader(make_rows(5))).status == "succeeded"
    assert_complete_and_unduplicated(engine, 5)


def test_database_connection_dropped_midway_leaves_consistent_data_and_rerun_heals(
    engine: Engine,
) -> None:
    n = 40
    state = {"inserts": 0}

    def kill_own_backend(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("INSERT INTO DONATIONS"):
            state["inserts"] += 1
            if state["inserts"] == 2:
                # Real connection loss: the server terminates this very session.
                cursor.execute("SELECT pg_terminate_backend(pg_backend_pid())")

    event.listen(engine, "before_cursor_execute", kill_own_backend)
    try:
        first = sync(engine, ListReader(make_rows(n)))
    finally:
        event.remove(engine, "before_cursor_execute", kill_own_backend)
    assert first.status in ("partial", "failed")
    committed = int(str(rows(engine, "SELECT count(*) FROM donations")[0][0]))
    assert committed % 10 == 0 and committed < n  # only whole batches survive
    assert sync(engine, ListReader(make_rows(n))).status == "succeeded"
    assert_complete_and_unduplicated(engine, n)


def test_lock_connection_lost_aborts_the_run_instead_of_continuing_unprotected(
    engine: Engine,
) -> None:
    n = 30
    state = {"killed": False}

    def kill_lock_holder(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("INSERT INTO DONATIONS") and not state["killed"]:
            state["killed"] = True
            with engine.begin() as other:
                other.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype='advisory' "
                        "AND objid=:k AND granted"
                    ),
                    {"k": LOCK_KEYS["sheet"]},
                )

    event.listen(engine, "before_cursor_execute", kill_lock_holder)
    try:
        res = sync(engine, ListReader(make_rows(n)))
    finally:
        event.remove(engine, "before_cursor_execute", kill_lock_holder)
    assert res.status in ("partial", "failed")
    assert int(str(rows(engine, "SELECT count(*) FROM donations")[0][0])) < n
    assert sync(engine, ListReader(make_rows(n))).status == "succeeded"
    assert_complete_and_unduplicated(engine, n)


def test_two_syncs_started_together_run_one_at_a_time_and_produce_no_duplicates(
    engine: Engine,
) -> None:
    n = 60
    results: list[SyncResult] = []
    barrier = threading.Barrier(2)

    class SlowReader(ListReader):
        def read(self) -> SheetTable:
            time.sleep(1.0)  # hold the lock long enough for the other to collide
            return super().read()

    def worker() -> None:
        eng = make_engine(database_url_for_tests())
        try:
            barrier.wait(timeout=10)
            results.append(sync(eng, SlowReader(make_rows(n))))
        finally:
            eng.dispose()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(r.status for r in results)
    # The reader sleeps 1s while holding the lock, so the other run must be locked out.
    assert statuses == ["locked", "succeeded"]
    assert_complete_and_unduplicated(engine, n)
    locked = [r for r in results if r.status == "locked"]
    for r in locked:
        assert rows(engine, f"SELECT status FROM sync_runs WHERE id={r.run_id}") == [("failed",)]


def test_a_second_sync_while_the_lock_is_held_does_nothing(engine: Engine) -> None:
    holder = engine.connect()
    try:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEYS["sheet"]})
        res = sync(engine, ListReader(make_rows(5)))
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEYS["sheet"]})
        holder.close()
    assert res.status == "locked"
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    assert sync(engine, ListReader(make_rows(5))).status == "succeeded"


def test_lock_is_released_after_a_failed_run_so_the_next_run_can_start(engine: Engine) -> None:
    bad = ListReader([], header=["nope"])
    assert sync(engine, bad).status == "failed"
    assert sync(engine, ListReader(make_rows(3))).status == "succeeded"
    assert rows(engine, "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted") == [
        (0,)
    ]


def test_stale_running_row_from_a_crashed_run_is_marked_failed_by_the_next_run(
    engine: Engine,
) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sync_runs (source, status) VALUES ('sheet', 'running')"))
    sync(engine, ListReader(make_rows(2)))
    got = rows(engine, "SELECT status, error_message IS NOT NULL FROM sync_runs ORDER BY id")
    assert got == [("failed", True), ("succeeded", False)]


def test_process_killed_with_sigkill_midway_then_rerun_has_no_loss_or_duplicates(
    engine: Engine, tmp_path: Path
) -> None:
    n = 50
    data = tmp_path / "sheet.csv"
    lines = [",".join(HEADER)] + [",".join(r) for r in make_rows(n)]
    data.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script = tmp_path / "crashing_sync.py"
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
            seen = {{"n": 0}}

            @event.listens_for(engine, "after_cursor_execute")
            def die(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("INSERT INTO DONATIONS"):
                    seen["n"] += 1
                    if seen["n"] == 3:
                        os._exit(137)  # like kill -9: no cleanup, no finally blocks

            run_sync(engine, CsvSheetReader({str(data)!r}), "America/Los_Angeles", 10,
                     now=datetime(2025, 6, 1, tzinfo=UTC))
            """
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(Path.cwd())}
    proc = subprocess.run([sys.executable, str(script)], env=env, capture_output=True)  # noqa: S603
    assert proc.returncode == 137, proc.stderr.decode()[-500:]
    crashed = rows(engine, "SELECT status FROM sync_runs")
    assert crashed == [("running",)]
    assert rows(engine, "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted") == [
        (0,)
    ]
    result = sync(engine, ListReader(make_rows(n)))
    assert result.status == "succeeded"
    assert_complete_and_unduplicated(engine, n)
    statuses = [r[0] for r in rows(engine, "SELECT status FROM sync_runs ORDER BY id")]
    assert statuses == ["failed", "succeeded"]


def test_cli_exit_codes_follow_the_documented_contract(
    engine: Engine, tmp_path: Path, settings: Settings
) -> None:
    data = tmp_path / "sheet.csv"
    data.write_text(",".join(HEADER) + "\n" + ",".join(good("CLI1")) + "\n", encoding="utf-8")
    env = {
        **os.environ,
        "DATABASE_URL": database_url_for_tests(),
        "SHEET_SOURCE": "csv",
        "SHEET_CSV_PATH": str(data),
    }
    ok = subprocess.run(
        [sys.executable, "-m", "donation_ledger.sync"], env=env, capture_output=True
    )  # noqa: S603
    assert ok.returncode == 0, ok.stderr.decode()
    data.write_text("wrong,header\n", encoding="utf-8")
    bad = subprocess.run(
        [sys.executable, "-m", "donation_ledger.sync"], env=env, capture_output=True
    )  # noqa: S603
    assert bad.returncode == 1
    assert b'"level"' in bad.stderr  # structured JSON logs
