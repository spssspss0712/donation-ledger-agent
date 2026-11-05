"""Sheet sync + the version 2 contact rules, on a real PostgreSQL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy import event as sa_event

from donation_ledger.sources import CsvSheetReader
from tests.test_sync import HEADER, ListReader, good, rows, sync

SAMPLE = Path("sample_data/sheet.csv")
UPDATE = Path("sample_data/sheet_update.csv")


def row(
    entry: str,
    name: str = "",
    email: str = "",
    address: str = "",
    amount: str = "10",
    recipient: str = "",
) -> list[str]:
    return [entry, "2024-03-01", name, email, address, amount, "USD", recipient]


# --- header ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        [h for h in HEADER if h != "Donor Email"],
        [*HEADER, "Donor Email"],
        ["Donor  E-mail" if h == "Donor Email" else h for h in HEADER],
    ],
    ids=["missing", "duplicated", "renamed"],
)
def test_header_without_exactly_one_donor_email_column_fails_the_run_and_writes_nothing(
    engine: Engine, header: list[str]
) -> None:
    res = sync(engine, ListReader([row("H1", email="a@b.org")], header=header))
    assert res.status == "failed" and "donor email" in (res.error or "")
    for table in ("donations", "rejected_rows", "sync_issues", "donation_contact_changes"):
        assert rows(engine, f"SELECT count(*) FROM {table}") == [(0,)]
    assert rows(engine, "SELECT status, rows_read FROM sync_runs") == [("failed", 0)]


# --- contact information never blocks a donation ---------------------------------------------


def test_missing_malformed_and_hostile_emails_never_quarantine_a_sheet_donation(
    engine: Engine,
) -> None:
    data = [
        row("E1"),
        row("E2", email="not-an-email"),
        row("E3", email="a@@b.org"),
        row("E4", email="x" * 300 + "@b.org"),
        row("E5", email="bad\x00@b.org"),
        row("E6", email="caf�@b.org"),
        row("E7", email=" OK@B.ORG "),
    ]
    res = sync(engine, ListReader(data))
    assert res.status == "succeeded"
    assert (res.counters.rows_inserted, res.counters.rows_quarantined) == (7, 0)
    assert rows(engine, "SELECT external_id, donor_email FROM donations ORDER BY 1") == [
        ("E1", None),
        ("E2", None),
        ("E3", None),
        ("E4", None),
        ("E5", None),
        ("E6", None),
        ("E7", "ok@b.org"),
    ]
    assert rows(
        engine, "SELECT external_id FROM sync_issues WHERE kind='invalid_email' ORDER BY 1"
    ) == [
        ("E2",),
        ("E3",),
        ("E4",),
        ("E5",),
        ("E6",),
    ]
    # The original text stays in raw_payload.
    assert rows(
        engine, "SELECT raw_payload->'fields'->>'donor_email' FROM donations WHERE external_id='E2'"
    ) == [("not-an-email",)]


def test_invalid_email_warning_is_recorded_once_across_reruns(engine: Engine) -> None:
    for _ in range(3):
        sync(engine, ListReader([row("W1", email="nope")]))
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(1,)]


# --- fill-once through the sync ---------------------------------------------------------------


def test_second_csv_fills_empty_contacts_once_keeps_existing_ones_and_never_touches_money(
    engine: Engine,
) -> None:
    sync(engine, CsvSheetReader(SAMPLE))
    money_before = rows(
        engine,
        "SELECT external_id, amount_minor, currency, donated_at, source FROM donations ORDER BY 1",
    )
    res = sync(engine, CsvSheetReader(UPDATE))
    assert res.status == "succeeded"
    # S-002: name, email and address; S-003: email; S-012: email  => 5 fills
    assert res.counters.contacts_filled == 5
    assert rows(
        engine,
        "SELECT external_id, donor_name, is_anonymous, donor_email, donor_address FROM donations "
        "WHERE external_id IN ('S-001','S-002','S-003','S-012') ORDER BY 1",
    ) == [
        ("S-001", "Alice Johnson", False, "alice@example.org", "12 Oak St, Portland, OR"),
        ("S-002", "Carol Diaz", False, "carol@example.org", "3 Pine St"),
        ("S-003", None, True, "anon.donor@example.org", "PO Box 5"),
        ("S-012", "Jun Sato", False, "jun@example.jp", "Tokyo"),
    ]
    assert (
        rows(
            engine,
            "SELECT external_id, amount_minor, currency, donated_at, source FROM donations ORDER BY 1",
        )
        == money_before
    )
    # S-001 already had a different email: kept, flagged. S-004's amount changed: kept, flagged.
    assert rows(
        engine,
        "SELECT external_id, kind FROM sync_issues WHERE kind <> 'invalid_email' ORDER BY 1, 2",
    ) == [("S-001", "contact_changed_after_sync"), ("S-004", "changed_after_sync")]
    assert rows(
        engine,
        "SELECT d.external_id, c.field_name, c.source, c.sync_run_id = r.id "
        "FROM donation_contact_changes c JOIN donations d ON d.id = c.donation_id "
        "CROSS JOIN (SELECT max(id) AS id FROM sync_runs) r ORDER BY 1, 2",
    ) == [
        ("S-002", "donor_address", "sheet", True),
        ("S-002", "donor_email", "sheet", True),
        ("S-002", "donor_name", "sheet", True),
        ("S-003", "donor_email", "sheet", True),
        ("S-012", "donor_email", "sheet", True),
    ]
    assert rows(engine, "SELECT contacts_filled FROM sync_runs ORDER BY id") == [(0,), (5,)]
    # S-013 still has the same malformed email: no new warning.
    assert rows(engine, "SELECT count(*) FROM sync_issues WHERE kind='invalid_email'") == [(2,)]


def test_rerunning_the_second_csv_is_a_no_op(engine: Engine) -> None:
    sync(engine, CsvSheetReader(SAMPLE))
    sync(engine, CsvSheetReader(UPDATE))
    snapshot = [
        rows(engine, f"SELECT * FROM {t} ORDER BY id")
        for t in ("donations", "donation_contact_changes", "sync_issues")
    ]
    again = sync(engine, CsvSheetReader(UPDATE))
    assert (again.counters.contacts_filled, again.counters.issues_recorded) == (0, 0)
    assert snapshot == [
        rows(engine, f"SELECT * FROM {t} ORDER BY id")
        for t in ("donations", "donation_contact_changes", "sync_issues")
    ]


def test_going_back_to_the_first_csv_never_blanks_filled_contacts(engine: Engine) -> None:
    sync(engine, CsvSheetReader(SAMPLE))
    sync(engine, CsvSheetReader(UPDATE))
    before = rows(
        engine, "SELECT id, donor_name, donor_email, donor_address FROM donations ORDER BY id"
    )
    res = sync(engine, CsvSheetReader(SAMPLE))
    assert res.counters.contacts_filled == 0
    assert before == rows(
        engine, "SELECT id, donor_name, donor_email, donor_address FROM donations ORDER BY id"
    )


def test_a_row_whose_amount_was_edited_still_fills_contacts_but_never_changes_money(
    engine: Engine,
) -> None:
    sync(engine, ListReader([row("X1", amount="10")]))
    res = sync(engine, ListReader([row("X1", name="Ann", email="a@b.org", amount="9999")]))
    # Same Entry ID = same donation (SPEC 2.3): contacts are filled, money is untouched,
    # and the money difference is reported for a human.
    assert res.counters.contacts_filled == 2
    assert rows(engine, "SELECT donor_name, donor_email, amount_minor FROM donations") == [
        ("Ann", "a@b.org", 1000)
    ]
    assert rows(engine, "SELECT kind FROM sync_issues") == [("changed_after_sync",)]


def test_reformatting_a_date_or_amount_without_changing_its_value_is_not_a_change(
    engine: Engine,
) -> None:
    first = ["R1", "2024-03-01", "", "", "", "1000", "USD", ""]
    second = ["R1", "3/1/2024", "", "", "", "$1,000.00", "usd", ""]
    sync(engine, ListReader([first]))
    res = sync(engine, ListReader([second]))
    assert res.counters.issues_recorded == 0
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(0,)]


def test_a_real_change_of_date_or_currency_is_reported_with_the_parsed_field_names(
    engine: Engine,
) -> None:
    sync(engine, ListReader([["D1", "2024-03-01", "", "", "", "10", "USD", ""]]))
    sync(engine, ListReader([["D1", "2024-03-02", "", "", "", "10", "EUR", ""]]))
    assert rows(engine, "SELECT detail->'changed_fields' FROM sync_issues") == [
        (["currency", "donated_at"],)
    ]


def test_the_word_anonymous_or_a_blank_cell_never_fills_or_clears_a_name(engine: Engine) -> None:
    sync(engine, ListReader([row("A1", name="Ann"), row("A2")]))
    sync(engine, ListReader([row("A1", name="Anonymous"), row("A2", name="ANONYMOUS")]))
    assert rows(engine, "SELECT donor_name, is_anonymous FROM donations ORDER BY external_id") == [
        ("Ann", False),
        (None, True),
    ]
    assert rows(engine, "SELECT count(*) FROM donation_contact_changes") == [(0,)]


def test_email_case_and_spacing_differences_are_not_a_change(engine: Engine) -> None:
    sync(engine, ListReader([row("C1", email="ann@b.org")]))
    res = sync(engine, ListReader([row("C1", email="  ANN@B.ORG ")]))
    assert res.counters.issues_recorded == 0


# --- whole-batch atomicity (SPEC 2.4) ---------------------------------------------------------


def _fail_on(engine: Engine, table_prefix: str, after: int = 0) -> Any:
    """Raise when the (after+1)-th INSERT INTO <table_prefix> runs; returns the remover."""
    seen = {"n": 0}

    def boom(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith(f"INSERT INTO {table_prefix.upper()}"):
            seen["n"] += 1
            if seen["n"] > after:
                raise RuntimeError("injected failure")

    sa_event.listen(engine, "before_cursor_execute", boom)
    return lambda: sa_event.remove(engine, "before_cursor_execute", boom)


ALL_TABLES = ("donations", "rejected_rows", "sync_issues", "donation_contact_changes")


def _snapshot(engine: Engine) -> dict[str, list[tuple[object, ...]]]:
    snap = {t: rows(engine, f"SELECT * FROM {t} ORDER BY id") for t in ALL_TABLES}
    snap["counters"] = rows(
        engine,
        "SELECT id, status, rows_read, rows_inserted, rows_quarantined, contacts_filled "
        "FROM sync_runs ORDER BY id",
    )
    return snap


def test_a_failing_batch_leaves_no_donations_fills_audit_rows_warnings_or_quarantine_records(
    engine: Engine,
) -> None:
    sync(engine, ListReader([row("B1"), row("B2", amount="10")]))
    before = _snapshot(engine)
    second = [
        row("B1", name="Late Name", email="late@b.org"),  # fills (the audit insert will fail)
        row("B2", amount="55"),  # money changed: changed_after_sync warning
        row("B3", email="nope"),  # new donation + invalid_email warning
        row("B4", amount="not money"),  # quarantine record
    ]
    remove = _fail_on(engine, "donation_contact_changes")
    try:
        res = sync(engine, ListReader(second))
    finally:
        remove()
    assert res.status == "failed"
    after = _snapshot(engine)
    runs_before, runs_after = before.pop("counters"), after.pop("counters")
    assert after == before  # every table the batch writes to is exactly as before
    assert runs_after[:-1] == runs_before  # the failed run only added its own bookkeeping row
    assert runs_after[-1][1:] == ("failed", 0, 0, 0, 0)
    # Rerun converges to the full, correct result.
    ok = sync(engine, ListReader(second))
    assert ok.status == "succeeded"
    assert rows(
        engine, "SELECT external_id, donor_email, donor_name FROM donations ORDER BY 1"
    ) == [
        ("B1", "late@b.org", "Late Name"),
        ("B2", None, None),
        ("B3", None, None),
    ]
    assert rows(engine, "SELECT external_id FROM rejected_rows") == [("B4",)]


def test_a_failure_in_a_later_batch_keeps_earlier_batches_whole_and_loses_nothing_on_rerun(
    engine: Engine,
) -> None:
    base = [row(f"P{i:02d}") for i in range(6)]
    sync(engine, ListReader(base))
    filled = [row(f"P{i:02d}", email=f"p{i}@b.org") for i in range(6)]
    # batch size 3: the first batch's 3 fills commit, the second batch's first audit insert fails.
    remove = _fail_on(engine, "donation_contact_changes", after=3)
    try:
        res = sync(engine, ListReader(filled), batch_size=3)
    finally:
        remove()
    assert res.status == "partial"
    assert rows(engine, "SELECT count(*) FROM donation_contact_changes") == [(3,)]
    assert rows(engine, "SELECT count(*) FROM donations WHERE donor_email IS NOT NULL") == [(3,)]
    assert rows(
        engine, "SELECT contacts_filled, status FROM sync_runs ORDER BY id DESC LIMIT 1"
    ) == [(3, "partial")]
    sync(engine, ListReader(filled), batch_size=3)
    assert rows(engine, "SELECT count(*) FROM donations WHERE donor_email IS NOT NULL") == [(6,)]
    assert rows(engine, "SELECT count(*) FROM donation_contact_changes") == [(6,)]


def test_a_failing_batch_also_rolls_back_quarantine_records_written_in_that_batch(
    engine: Engine,
) -> None:
    data = [row("Q1", amount="bad"), row("Q2"), row("Q3", amount="worse")]
    remove = _fail_on(engine, "donations")
    try:
        res = sync(engine, ListReader(data))
    finally:
        remove()
    assert res.status == "failed"
    assert rows(engine, "SELECT count(*) FROM rejected_rows") == [(0,)]
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]


def test_good_helper_rows_still_have_eight_cells() -> None:
    assert len(good("Z")) == len(HEADER) == 8
