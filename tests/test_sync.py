from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from donation_ledger.runs import Counters
from donation_ledger.sheet_rows import HeaderError
from donation_ledger.sources import CsvSheetReader, SheetReadError, SheetTable
from donation_ledger.sync import SyncResult, run_sync

TZ = "America/Los_Angeles"
HEADER = [
    "Entry ID",
    "Date",
    "Donor Name",
    "Donor Email",
    "Donor Address",
    "Amount",
    "Currency",
    "Recipient",
]
NOW = datetime(2025, 6, 1, tzinfo=UTC)
SAMPLE = Path("sample_data/sheet.csv")


class ListReader:
    """Fake at the external boundary only (the Google Sheet)."""

    def __init__(self, rows: list[list[str]], header: list[str] | None = None) -> None:
        self.table = SheetTable(header=header or HEADER, rows=rows)

    def read(self) -> SheetTable:
        return self.table


def sync(engine: Engine, reader: object, batch_size: int = 500) -> SyncResult:
    return run_sync(engine, reader, TZ, batch_size, now=NOW)  # type: ignore[arg-type]


def rows(engine: Engine, sql: str) -> list[tuple[object, ...]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql))]


def good(
    entry: str, amount: str = "10", name: str = "Pat Doe", email: str = "pat@example.org"
) -> list[str]:
    return [entry, "2024-03-01", name, email, "1 Road", amount, "USD", "Fund"]


def test_sample_csv_inserts_good_rows_and_quarantines_bad_ones_with_reasons(engine: Engine) -> None:
    res = sync(engine, CsvSheetReader(SAMPLE))
    assert res.status == "succeeded"
    donations = rows(
        engine,
        "SELECT external_id, donor_name, is_anonymous, amount_minor, currency, "
        "donated_at::text FROM donations ORDER BY external_id",
    )
    assert donations == [
        ("S-001", "Alice Johnson", False, 10000, "USD", "2024-03-01 08:00:00+00"),
        ("S-002", None, True, 100050, "USD", "2024-03-02 08:00:00+00"),
        ("S-003", None, True, 2550, "USD", "2024-03-03 22:30:00+00"),
        ("S-004", "Bob Lee", False, 100000, "EUR", "2024-03-04 17:15:00+00"),
        ("S-012", "Jun Sato", False, 5000, "JPY", "2024-03-12 07:00:00+00"),
        ("S-013", "Kim Nguyen", False, 1999, "USD", "2025-01-01 07:30:00+00"),
        ("S-015", "Mia Costa", False, 4000, "USD", "2024-03-15 07:00:00+00"),
    ]
    # Emails: trimmed and lowercased; missing -> NULL; malformed ("not-an-email", no dot in
    # the domain) -> NULL with the donation still stored.
    assert rows(engine, "SELECT external_id, donor_email FROM donations ORDER BY external_id") == [
        ("S-001", "alice@example.org"),
        ("S-002", None),
        ("S-003", None),
        ("S-004", "bob@example.org"),
        ("S-012", None),
        ("S-013", None),
        ("S-015", "mia@example.org"),
    ]
    assert rows(engine, "SELECT external_id, kind FROM sync_issues ORDER BY external_id") == [
        ("S-003", "invalid_email"),
        ("S-013", "invalid_email"),
    ]
    rejected = rows(
        engine,
        "SELECT external_id, split_part(reason, ':', 1) FROM rejected_rows ORDER BY id",
    )
    assert rejected == [
        ("S-005", "invalid_amount"),
        ("S-006", "invalid_amount"),
        ("S-007", "invalid_date"),
        ("S-008", "duplicate_entry_id"),
        ("S-008", "duplicate_entry_id"),
        (None, "missing_entry_id"),
        ("S-010", "invalid_amount"),
        ("S-011", "invalid_currency"),
        ("S-014", "malformed_row"),
    ]
    assert (res.counters.rows_read, res.counters.rows_inserted, res.counters.rows_quarantined) == (
        16,
        7,
        9,
    )
    run = rows(engine, "SELECT status, rows_read, rows_inserted, rows_quarantined FROM sync_runs")
    assert run == [("succeeded", 16, 7, 9)]


def test_the_word_anonymous_is_never_stored(engine: Engine) -> None:
    sync(engine, CsvSheetReader(SAMPLE))
    assert rows(engine, "SELECT count(*) FROM donations WHERE donor_name ILIKE '%anonymous%'") == [
        (0,)
    ]


def test_rerunning_the_same_sheet_changes_nothing_and_reports_duplicates(engine: Engine) -> None:
    sync(engine, CsvSheetReader(SAMPLE))
    before = rows(engine, "SELECT * FROM donations ORDER BY id")
    second = sync(engine, CsvSheetReader(SAMPLE))
    assert second.status == "succeeded"
    assert rows(engine, "SELECT * FROM donations ORDER BY id") == before
    assert second.counters.rows_inserted == 0
    assert second.counters.rows_duplicate_skipped == 7
    assert second.counters.issues_recorded == 0  # the two invalid_email warnings are not repeated
    # Quarantined rows are re-evaluated every run, not skipped forever: one row each.
    assert rows(engine, "SELECT count(*), max(attempts) FROM rejected_rows") == [(9, 2)]


def test_rows_added_later_are_picked_up_without_touching_existing_ones(engine: Engine) -> None:
    reader = ListReader([good("A1")])
    sync(engine, reader)
    reader.table = SheetTable(HEADER, [good("A1"), good("A2", "20")])
    res = sync(engine, reader)
    assert res.counters.rows_inserted == 1
    assert rows(engine, "SELECT external_id FROM donations ORDER BY id") == [("A1",), ("A2",)]


def test_quarantined_row_is_retried_and_ingested_once_it_is_fixed(engine: Engine) -> None:
    reader = ListReader([good("B1", "oops")])
    sync(engine, reader)
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    reader.table = SheetTable(HEADER, [good("B1", "12.34")])
    res = sync(engine, reader)
    assert res.counters.rows_inserted == 1
    assert rows(engine, "SELECT amount_minor FROM donations") == [(1234,)]
    assert rows(engine, "SELECT status FROM rejected_rows") == [("resolved",)]


def test_a_quarantined_row_removed_from_the_sheet_is_marked_absent_not_resolved(
    engine: Engine,
) -> None:
    reader = ListReader([good("C1", "bad")])
    sync(engine, reader)
    reader.table = SheetTable(HEADER, [])
    sync(engine, reader)
    assert rows(engine, "SELECT status FROM rejected_rows") == [("absent",)]


def test_a_rejection_that_comes_back_is_reopened(engine: Engine) -> None:
    bad = ListReader([good("D1", "bad")])
    sync(engine, bad)
    sync(engine, ListReader([]))
    sync(engine, bad)
    assert rows(engine, "SELECT status, attempts FROM rejected_rows") == [("open", 2)]


def test_manually_ignored_rejection_stays_ignored_and_is_never_inserted(engine: Engine) -> None:
    reader = ListReader([good("E1", "bad")])
    sync(engine, reader)
    with engine.begin() as conn:
        conn.execute(text("UPDATE rejected_rows SET status='ignored'"))
    res = sync(engine, reader)
    assert rows(engine, "SELECT status FROM rejected_rows") == [("ignored",)]
    assert res.counters.rows_ignored == 1 and res.counters.rows_quarantined == 0


def test_duplicate_entry_ids_quarantine_every_copy_and_insert_none(engine: Engine) -> None:
    sync(engine, ListReader([good("F1", "5"), good("F1", "6"), good("F2")]))
    assert rows(engine, "SELECT external_id FROM donations") == [("F2",)]
    assert rows(engine, "SELECT count(*) FROM rejected_rows WHERE external_id='F1'") == [(2,)]


def test_empty_entry_id_is_quarantined_and_never_written(engine: Engine) -> None:
    sync(engine, ListReader([good(""), good("   ")]))
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    assert rows(engine, "SELECT count(*) FROM rejected_rows") == [(2,)]


def test_edit_after_sync_only_records_a_warning_and_leaves_the_donation_untouched(
    engine: Engine,
) -> None:
    reader = ListReader([good("G1", "10")])
    sync(engine, reader)
    reader.table = SheetTable(HEADER, [good("G1", "9999")])
    res = sync(engine, reader)
    assert rows(engine, "SELECT amount_minor FROM donations") == [(1000,)]
    issues = rows(engine, "SELECT external_id, kind, detail->'changed_fields' FROM sync_issues")
    assert issues == [("G1", "changed_after_sync", ["amount_minor"])]
    assert res.counters.issues_recorded == 1


def test_the_same_edit_is_warned_about_only_once_across_runs(engine: Engine) -> None:
    sync(engine, ListReader([good("H1", "10")]))
    edited = ListReader([good("H1", "11")])
    sync(engine, edited)
    sync(engine, edited)
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(1,)]


def test_row_edited_into_something_invalid_after_sync_is_a_warning_not_a_quarantine(
    engine: Engine,
) -> None:
    sync(engine, ListReader([good("I1", "10")]))
    sync(engine, ListReader([good("I1", "garbage")]))
    assert rows(engine, "SELECT count(*) FROM rejected_rows") == [(0,)]
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(1,)]
    assert rows(engine, "SELECT amount_minor FROM donations") == [(1000,)]


def test_whitespace_only_differences_do_not_raise_a_warning(engine: Engine) -> None:
    sync(engine, ListReader([good("J1", "10")]))
    sync(engine, ListReader([good("J1", " 10 ")]))
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(0,)]


def test_rows_deleted_from_the_sheet_are_ignored_and_the_donation_stays(engine: Engine) -> None:
    sync(engine, ListReader([good("K1"), good("K2")]))
    sync(engine, ListReader([good("K2")]))
    assert rows(engine, "SELECT external_id FROM donations ORDER BY id") == [("K1",), ("K2",)]
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(0,)]


def test_renamed_or_missing_column_fails_the_run_and_writes_no_donations(engine: Engine) -> None:
    header = [h if h != "Amount" else "Gift Amount" for h in HEADER]
    res = sync(engine, ListReader([good("L1")], header=header))
    assert res.status == "failed"
    assert "amount" in (res.error or "")
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    assert rows(engine, "SELECT status FROM sync_runs") == [("failed",)]


def test_header_matching_ignores_case_spacing_bom_and_column_order(engine: Engine) -> None:
    header = [
        "﻿ENTRY  id",
        " date",
        "Recipient",
        "amount ",
        "Donor Address",
        "currency",
        "donor name",
        "  DONOR   email",
    ]
    row = ["M1", "2024-03-01", "Fund", "5", "1 Road", "", "Zed", "Zed@X.org"]
    res = sync(engine, ListReader([row], header=header))
    assert res.status == "succeeded"
    assert rows(engine, "SELECT donor_name, recipient, currency, donor_email FROM donations") == [
        ("Zed", "Fund", "USD", "zed@x.org")
    ]


def test_duplicate_header_columns_fail_the_run(engine: Engine) -> None:
    res = sync(engine, ListReader([], header=[*HEADER, "Amount"]))
    assert res.status == "failed"


def test_extra_columns_are_kept_in_raw_payload_and_ignored_for_change_detection(
    engine: Engine,
) -> None:
    header = [*HEADER, "Notes"]
    sync(engine, ListReader([[*good("N1"), "first note"]], header=header))
    sync(engine, ListReader([[*good("N1"), "edited note"]], header=header))
    assert rows(engine, "SELECT raw_payload->'extra'->>'Notes' FROM donations") == [("first note",)]
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(0,)]


def test_truncated_rows_are_quarantined_but_explicitly_blank_trailing_cells_are_fine(
    engine: Engine,
) -> None:
    # "7" could be the cut-off start of "700": never ingest it as a 7 dollar donation.
    res = sync(
        engine,
        ListReader(
            [
                ["P1", "2024-03-01", "Pat", "", "", "7"],
                ["P2", "2024-03-01", "Pat", "", "", "7", "", ""],
            ]
        ),
    )
    assert res.counters.rows_inserted == 1 and res.counters.rows_quarantined == 1
    assert rows(engine, "SELECT external_id, currency, recipient FROM donations") == [
        ("P2", "USD", None)
    ]
    assert rows(engine, "SELECT external_id, reason FROM rejected_rows")[0][0] == "P1"


def test_nul_characters_quarantine_the_row_instead_of_crashing_the_batch(engine: Engine) -> None:
    res = sync(engine, ListReader([good("Q1", name="Bad\x00Name"), good("Q2")]))
    assert res.status == "succeeded"
    assert rows(engine, "SELECT external_id FROM donations") == [("Q2",)]
    assert rows(engine, "SELECT external_id FROM rejected_rows") == [("Q1",)]


def test_over_long_contact_text_is_truncated_and_over_long_ids_are_quarantined(
    engine: Engine,
) -> None:
    long_name = "x" * 100_000
    res = sync(
        engine,
        ListReader(
            [
                good("R1", name=long_name),
                good("R2"),
                ["R3" * 500, "2024-03-01", "a", "", "b", "5", "USD", "c"],
            ]
        ),
    )
    assert res.status == "succeeded"
    # R1's 100,000-character name is contact text: cut and flagged, never a reason to refuse
    # the donation (SPEC v2). R3's oversized Entry ID is not contact text and stays quarantined.
    assert rows(engine, "SELECT external_id FROM donations ORDER BY 1") == [("R1",), ("R2",)]
    assert rows(engine, "SELECT length(donor_name) FROM donations WHERE external_id='R1'") == [
        (500,)
    ]
    assert rows(engine, "SELECT count(*) FROM rejected_rows") == [(1,)]


def test_timezone_boundary_midnight_local_maps_to_the_correct_utc_instant(engine: Engine) -> None:
    sync(engine, ListReader([["T1", "2024-06-30", "A", "", "", "5", "USD", ""]]))
    assert rows(engine, "SELECT donated_at::text FROM donations") == [("2024-06-30 07:00:00+00",)]


def test_future_dated_row_is_quarantined_until_it_is_plausible(engine: Engine) -> None:
    res = sync(engine, ListReader([["U1", "2031-01-01", "A", "", "", "5", "USD", ""]]))
    assert res.counters.rows_quarantined == 1


def test_batches_are_committed_independently_and_counters_add_up(engine: Engine) -> None:
    data = [good(f"V{i:03d}") for i in range(25)]
    res = sync(engine, ListReader(data), batch_size=10)
    assert res.counters.rows_inserted == 25
    assert rows(engine, "SELECT rows_read, rows_inserted FROM sync_runs") == [(25, 25)]


def test_unreadable_sheet_marks_the_run_failed(engine: Engine, tmp_path: Path) -> None:
    res = sync(engine, CsvSheetReader(tmp_path / "missing.csv"))
    assert res.status == "failed"
    assert rows(engine, "SELECT status FROM sync_runs") == [("failed",)]
    with pytest.raises(SheetReadError):
        CsvSheetReader(tmp_path / "missing.csv").read()


def test_csv_with_utf8_bom_and_crlf_and_quoted_newlines_is_read(
    engine: Engine, tmp_path: Path
) -> None:
    p = tmp_path / "s.csv"
    p.write_bytes(
        b"\xef\xbb\xbfEntry ID,Date,Donor Name,Donor Email,Donor Address,Amount,Currency,Recipient\r\n"
        b'W1,2024-03-01,Zoe,,"1 Road\nApt 2",5,USD,Fund\r\n'
    )
    res = sync(engine, CsvSheetReader(p))
    assert res.counters.rows_inserted == 1
    assert rows(engine, "SELECT donor_address FROM donations") == [("1 Road Apt 2",)]


def test_empty_counters_object_adds_correctly() -> None:
    a, b = Counters(rows_read=1), Counters(rows_read=2, rows_inserted=1)
    a.add(b)
    assert (a.rows_read, a.rows_inserted) == (3, 1)


def test_header_error_type_is_exposed_for_callers() -> None:
    assert issubclass(HeaderError, Exception)
