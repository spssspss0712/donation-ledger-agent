"""Tests written to break the implementation (self-review round 1)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from donation_ledger.api import create_app
from donation_ledger.config import Settings
from donation_ledger.ledger import NewDonation, Rejection
from donation_ledger.stripe_mapping import map_payment_intent
from tests.conftest import FAKE_WEBHOOK_SECRET
from tests.helpers import encode, event, payment_intent, sign
from tests.test_sync import rows


@pytest.mark.parametrize(
    "overrides",
    [
        {"currency": "ǅǅǅ"},  # 3 letters per str.isalpha(), but not ASCII
        {"currency": "us"},
        {"created": float("nan")},
        {"created": float("inf")},
        {"created": 10**20},
        {"created": -(10**20)},
        {"created": True},
        {"amount": True, "amount_received": True},
        {"amount": 10**30, "amount_received": 10**30},
        {"amount": 12.5, "amount_received": None},
        {"id": "pi_" + "x" * 300},
        {"id": None},
        {"id": ["pi_1"]},
    ],
)
def test_hostile_payment_intent_values_are_quarantined_never_raised(
    overrides: dict[str, Any],
) -> None:
    result = map_payment_intent({**payment_intent(), **overrides})
    assert isinstance(result, Rejection)


def test_payment_intent_with_wrong_types_everywhere_does_not_crash_the_mapper() -> None:
    weird = payment_intent(
        metadata="not a dict", shipping=[1], latest_charge=7, charges={"data": "x"}, receipt_email=5
    )
    result = map_payment_intent(weird)
    assert isinstance(result, NewDonation) and result.donor_name is None


def test_overlong_stripe_donor_name_is_truncated_not_fatal() -> None:
    result = map_payment_intent(payment_intent(metadata={"donor_name": "n" * 10_000}))
    assert isinstance(result, NewDonation) and result.donor_name is not None
    assert len(result.donor_name) == 500


def test_non_ascii_currency_through_the_webhook_is_quarantined_with_200(
    engine: Engine, settings: Settings
) -> None:
    client = TestClient(create_app(settings, engine), raise_server_exceptions=False)
    body = encode(event(payment_intent(currency="ǅǅǅ")))
    resp = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sign(body, FAKE_WEBHOOK_SECRET)},
    )
    assert resp.status_code == 200
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    assert rows(engine, "SELECT count(*) FROM rejected_rows") == [(1,)]


def test_events_for_one_payment_arriving_out_of_order_still_yield_one_donation(
    engine: Engine, settings: Settings
) -> None:
    client = TestClient(create_app(settings, engine), raise_server_exceptions=False)
    sequence = [
        ("payment_intent.succeeded", "evt_2"),
        ("payment_intent.created", "evt_1"),  # created arrives after succeeded
        ("charge.succeeded", "evt_3"),
        ("payment_intent.succeeded", "evt_2"),  # redelivery
        ("payment_intent.canceled", "evt_4"),  # late nonsense must not remove or alter anything
    ]
    for type_, evt_id in sequence:
        body = encode(event(payment_intent(), event_id=evt_id, type_=type_))
        resp = client.post(
            "/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": sign(body, FAKE_WEBHOOK_SECRET)},
        )
        assert resp.status_code == 200
    assert rows(engine, "SELECT external_id, amount_minor FROM donations") == [
        ("pi_FAKE0001", 2500)
    ]


def test_stripe_and_sheet_ids_that_look_alike_do_not_collide(engine: Engine) -> None:
    with engine.begin() as conn:
        for source in ("stripe", "sheet"):
            conn.execute(
                text(
                    "INSERT INTO donations (source, external_id, amount_minor, currency, donated_at,"
                    " is_anonymous, raw_payload) VALUES (:s, 'same-id', 5, 'USD', now(), true, '{}')"
                ),
                {"s": source},
            )
    assert rows(engine, "SELECT count(*) FROM donations") == [(2,)]


def test_database_constraints_reject_bad_rows_even_if_application_code_is_bypassed(
    engine: Engine,
) -> None:
    from sqlalchemy.exc import IntegrityError

    base = (
        "INSERT INTO donations (source, external_id, amount_minor, currency, donated_at, "
        "donor_name, is_anonymous, raw_payload) VALUES "
    )
    bad = [
        "('stripe', 'a', 0, 'USD', now(), NULL, true, '{}')",
        "('stripe', 'b', -1, 'USD', now(), NULL, true, '{}')",
        "('stripe', 'c', 1, 'usd', now(), NULL, true, '{}')",
        "('stripe', 'd', 1, 'USD', now(), 'Anonymous', true, '{}')",
        "('stripe', 'e', 1, 'USD', now(), NULL, false, '{}')",
        "('paypal', 'f', 1, 'USD', now(), NULL, true, '{}')",
        "('stripe', '', 1, 'USD', now(), NULL, true, '{}')",
    ]
    for values in bad:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(base + values))
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]


def test_non_finite_numbers_in_a_signed_payload_are_stored_safely_not_a_500(
    engine: Engine, settings: Settings
) -> None:
    client = TestClient(create_app(settings, engine), raise_server_exceptions=False)
    body = encode(event(payment_intent(created=float("nan"))))  # json.dumps emits bare NaN
    resp = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sign(body, FAKE_WEBHOOK_SECRET)},
    )
    assert resp.status_code == 200
    assert rows(engine, "SELECT count(*) FROM rejected_rows") == [(1,)]


def test_the_word_anonymous_appears_nowhere_in_stored_sheet_data(engine: Engine) -> None:
    from donation_ledger.sync import run_sync
    from tests.test_sync import TZ, ListReader, good

    run_sync(
        engine,
        ListReader([good("AN1", name="Anonymous"), good("AN2", "bad", name="ANONYMOUS")]),
        TZ,
    )
    run_sync(
        engine,
        ListReader([good("AN1", name="anonymous"), good("AN2", "bad", name="ANONYMOUS")]),
        TZ,
    )
    dump = rows(
        engine,
        "SELECT (SELECT string_agg(raw_payload::text, '') FROM donations) || "
        "(SELECT string_agg(raw_row::text, '') FROM rejected_rows) || "
        "coalesce((SELECT string_agg(detail::text, '') FROM sync_issues), '')",
    )[0][0]
    assert "anonymous" not in str(dump).lower()
    # Re-casing "Anonymous" is not an edit worth a warning.
    assert rows(engine, "SELECT count(*) FROM sync_issues") == [(0,)]


def test_signed_json_array_body_is_a_400_not_a_500(engine: Engine, settings: Settings) -> None:
    client = TestClient(create_app(settings, engine), raise_server_exceptions=False)
    body = b"[]"
    resp = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sign(body, FAKE_WEBHOOK_SECRET)},
    )
    assert resp.status_code == 400


def test_succeeded_payment_with_zero_amount_received_is_quarantined_not_charged_full_amount() -> (
    None
):
    obj = payment_intent(amount=2000)
    obj["amount_received"] = 0
    assert isinstance(map_payment_intent(obj), Rejection)


def test_database_connections_carry_timeouts(engine: Engine) -> None:
    with engine.connect() as conn:
        assert rows_one(conn, "SHOW statement_timeout") == "2min"
        assert rows_one(conn, "SHOW lock_timeout") == "30s"


def rows_one(conn: Any, sql: str) -> str:
    return str(conn.execute(text(sql)).scalar_one())


@pytest.mark.parametrize(
    "cell",
    [
        "12/31/9999 23:00",
        "9999-12-31 23:59",
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:00:00-05:00",
    ],
)
def test_extreme_dates_are_quarantined_and_do_not_fail_the_whole_sync(
    engine: Engine, cell: str
) -> None:
    from donation_ledger.sync import run_sync
    from tests.test_sync import TZ, ListReader, good

    bad = ["EX2", cell, "A", "", "5", "USD", ""]
    res = run_sync(engine, ListReader([good("EX1"), bad, good("EX3")]), TZ)
    assert res.status == "succeeded"
    assert rows(engine, "SELECT external_id FROM donations ORDER BY 1") == [("EX1",), ("EX3",)]
    assert rows(engine, "SELECT external_id FROM rejected_rows") == [("EX2",)]


def test_oversized_batch_size_is_refused_at_configuration_time() -> None:
    from donation_ledger.config import ConfigError

    with pytest.raises(ConfigError):
        Settings.from_env({"DATABASE_URL": "postgresql://h/d", "SYNC_BATCH_SIZE": "7000"})


def test_stripe_payload_never_stores_the_word_anonymous_and_unknown_currency_is_quarantined(
    engine: Engine, settings: Settings
) -> None:
    client = TestClient(create_app(settings, engine), raise_server_exceptions=False)
    named = payment_intent(latest_charge={"billing_details": {"name": "Anonymous"}})
    for obj in (named, payment_intent("pi_XYZ", currency="xyz")):
        body = encode(event(obj))
        hdr = {"Stripe-Signature": sign(body, FAKE_WEBHOOK_SECRET)}
        assert client.post("/webhooks/stripe", content=body, headers=hdr).status_code == 200
    assert rows(
        engine, "SELECT is_anonymous, raw_payload::text ILIKE '%anonymous%' FROM donations"
    ) == [(True, False)]
    assert rows(engine, "SELECT external_id FROM rejected_rows") == [("pi_XYZ",)]


def test_chunked_oversized_body_without_content_length_is_refused(
    engine: Engine, settings: Settings
) -> None:
    client = TestClient(create_app(settings, engine), raise_server_exceptions=False)

    def gen() -> Any:
        for _ in range(12):
            yield b"x" * 100_000

    resp = client.post("/webhooks/stripe", content=gen(), headers={"Stripe-Signature": "t=1,v1=00"})
    assert resp.status_code == 413


def test_fuzzed_sheet_cells_and_payment_intents_never_raise_out_of_the_classifiers() -> None:
    import random
    from datetime import UTC, datetime

    from donation_ledger.sheet_rows import classify_table
    from donation_ledger.sources import SheetTable

    rng = random.Random(1234)  # noqa: S311 - fixed seed, not security related
    alphabet = list("0123456789-/:.,$()+eE TZ\u0000\ud800٠１é‮ ") + ["9999", "00", "NaN"]

    def junk() -> str:
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 25)))

    header = ["Entry ID", "Date", "Donor Name", "Donor Address", "Amount", "Currency", "Recipient"]
    now = datetime(2025, 6, 1, tzinfo=UTC)
    for _ in range(3000):
        row = [junk() for _ in range(rng.randint(1, 9))]
        classify_table(SheetTable(header, [row]), "America/Los_Angeles", now)
    values: list[Any] = [None, True, 0, -1, 1.5, 10**30, "", "x", [], {}, float("nan"), "pi_1"]
    for _ in range(3000):
        pi = {
            k: rng.choice(values)
            for k in (
                "id",
                "amount",
                "amount_received",
                "currency",
                "created",
                "metadata",
                "latest_charge",
                "receipt_email",
                "shipping",
            )
        }
        map_payment_intent(pi)


def test_ignoring_one_duplicate_id_rejection_does_not_suppress_the_row_once_it_is_valid(
    engine: Engine,
) -> None:
    from donation_ledger.sync import run_sync
    from tests.test_sync import TZ, ListReader, good

    x, y = good("DUP", "5"), good("DUP", "7")
    run_sync(engine, ListReader([x, y]), TZ)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE rejected_rows SET status='ignored' WHERE reason LIKE 'duplicate%' AND raw_row->>'Amount'='5'"
            )
        )
    run_sync(engine, ListReader([x]), TZ)  # the other copy was deleted from the sheet
    assert rows(engine, "SELECT external_id, amount_minor FROM donations") == [("DUP", 500)]


def test_csv_with_a_bad_byte_and_an_oversized_cell_still_syncs_the_other_rows(
    engine: Engine, tmp_path: Any
) -> None:
    from donation_ledger.sources import CsvSheetReader
    from donation_ledger.sync import run_sync
    from tests.test_sync import HEADER, TZ

    p = tmp_path / "s.csv"
    big = "x" * 300_000
    p.write_bytes(
        (",".join(HEADER) + "\n").encode()
        + b"C1,2024-03-01,Jos\xe9,,5,USD,\n"
        + f"C2,2024-03-01,{big},,5,USD,\n".encode()
        + b"C3,2024-03-01,Ok,,5,USD,\n"
    )
    res = run_sync(engine, CsvSheetReader(p), TZ)
    assert res.status == "succeeded"
    # C1 has an undecodable byte: quarantined (not stored with U+FFFD); C2 is oversized.
    assert rows(engine, "SELECT external_id FROM donations ORDER BY 1") == [("C3",)]
    assert rows(engine, "SELECT external_id FROM rejected_rows ORDER BY 1") == [("C1",), ("C2",)]


def test_hostile_values_that_overflow_or_use_surrogates_are_quarantined_not_fatal() -> None:
    from datetime import UTC, datetime

    from donation_ledger.sheet_rows import Candidate, classify_table
    from donation_ledger.sources import SheetTable
    from tests.test_sync import HEADER

    now = datetime(2025, 6, 1, tzinfo=UTC)
    table = SheetTable(
        HEADER,
        [
            ["a1\ud800", "2024-03-01", "N", "", "5", "USD", ""],
            ["a2", "2024-03-01", "N", "", "9" * 1_000_005, "USD", ""],
            ["a3", "2024-03-01", "N", "", "5", "ınr", ""],
        ],
    )
    surrogate, huge, odd_currency = classify_table(table, "UTC", now)
    # A lone surrogate in the ID is replaced so the row stays storable.
    assert isinstance(surrogate, Candidate) and surrogate.entry_id == "a1?"
    assert isinstance(surrogate.result, NewDonation)
    for o in (huge, odd_currency):
        assert isinstance(o, Candidate) and isinstance(o.result, Rejection)


def test_api_rejects_out_of_range_query_parameters_with_422_not_500(
    engine: Engine, settings: Settings
) -> None:
    from tests.conftest import FAKE_API_TOKEN

    c = TestClient(create_app(settings, engine))
    h = {"Authorization": f"Bearer {FAKE_API_TOKEN}"}
    assert c.get("/donations?before_id=99999999999999999999", headers=h).status_code == 422
    assert c.get("/sync-runs?source=%00", headers=h).status_code == 422


def test_invisible_only_names_are_treated_as_anonymous() -> None:
    from donation_ledger.parsing import normalize_donor_name

    assert normalize_donor_name("​​") is None
    result = map_payment_intent(payment_intent(metadata={"donor_name": "​"}))
    assert isinstance(result, NewDonation) and result.donor_name is None


def test_invisible_characters_cannot_disguise_ids_names_or_currencies(engine: Engine) -> None:
    from donation_ledger.sync import run_sync
    from tests.test_sync import TZ, ListReader, good

    zw = "​"
    data = [
        good("1001", name="Bob"),
        good("1001" + zw, name="Bob"),  # same ID once invisible chars are ignored
        good(zw),  # "blank" ID
        good("2002", name="Anonymous" + zw),
        ["3003", "2024-03-01", "N", "", "5", zw, ""],  # invisible currency -> default USD
    ]
    run_sync(engine, ListReader(data), TZ)
    assert rows(engine, "SELECT external_id, donor_name, currency FROM donations ORDER BY 1") == [
        ("2002", None, "USD"),
        ("3003", "N", "USD"),
    ]
    assert rows(engine, "SELECT count(*) FROM rejected_rows") == [(3,)]


def test_absurdly_long_amount_digit_strings_are_rejected_quickly() -> None:
    import time

    from donation_ledger.parsing import RowError, minor_units_from_decimal_string

    start = time.monotonic()
    with pytest.raises(RowError):
        minor_units_from_decimal_string("1" * 900_000, "USD")
    assert time.monotonic() - start < 1


def test_duplicate_extra_header_names_keep_both_columns(engine: Engine) -> None:
    from donation_ledger.sync import run_sync
    from tests.test_sync import HEADER, TZ, ListReader, good

    run_sync(engine, ListReader([[*good("H1"), "n1", "n2"]], header=[*HEADER, "Note", "Note"]), TZ)
    assert rows(
        engine, "SELECT jsonb_object_keys(raw_payload->'extra') FROM donations ORDER BY 1"
    ) == [
        ("Note",),
        ("Note_9",),
    ]


def test_ignored_stripe_rejection_is_counted_as_ignored_by_reconcile(engine: Engine) -> None:
    from datetime import timedelta

    from donation_ledger.reconcile import run_reconcile

    class One:
        def list_since(self, created_gte: Any) -> Any:
            return [payment_intent("pi_BADIGN", amount=0)]

    run_reconcile(engine, One(), timedelta(hours=1))
    with engine.begin() as conn:
        conn.execute(text("UPDATE rejected_rows SET status='ignored'"))
    res = run_reconcile(engine, One(), timedelta(hours=1))
    assert (res.counters.rows_ignored, res.counters.rows_quarantined) == (1, 0)
    assert rows(engine, "SELECT attempts FROM rejected_rows") == [(2,)]


def test_alembic_works_from_any_working_directory(tmp_path: Any) -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    from tests.conftest import database_url_for_tests

    url = database_url_for_tests().rsplit("/", 1)[0] + "/ledger_demo"
    ini = Path("alembic.ini").resolve()
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(Path.cwd())}
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-c", str(ini), "current"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode()[-400:]


def test_non_utf8_bytes_are_quarantined_not_stored_as_replacement_characters(
    engine: Engine, tmp_path: Any
) -> None:
    from donation_ledger.sources import CsvSheetReader
    from donation_ledger.sync import run_sync
    from tests.test_sync import HEADER, TZ

    p = tmp_path / "s.csv"
    p.write_bytes((",".join(HEADER) + "\n").encode() + b"\xc9-1,2024-01-15,Jos\xe9,,50,,r\n")
    run_sync(engine, CsvSheetReader(p), TZ)
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    p.write_bytes((",".join(HEADER) + "\n").encode() + "É-1,2024-01-15,José,,50,,r\n".encode())
    run_sync(engine, CsvSheetReader(p), TZ)  # fixed export
    assert rows(engine, "SELECT external_id FROM donations") == [("É-1",)]


def test_invisible_characters_in_header_names_do_not_fail_the_run(engine: Engine) -> None:
    from donation_ledger.sync import run_sync
    from tests.test_sync import HEADER, TZ, ListReader, good

    header = [HEADER[0] + "​", *HEADER[1:]]
    assert run_sync(engine, ListReader([good("ZH1")], header=header), TZ).status == "succeeded"


def test_dollar_sign_contradicting_the_currency_is_quarantined(engine: Engine) -> None:
    from donation_ledger.parsing import RowError, minor_units_from_decimal_string

    assert minor_units_from_decimal_string("$5", "CAD") == 500
    for cur in ("GBP", "JPY", "EUR"):
        with pytest.raises(RowError):
            minor_units_from_decimal_string("$5", cur)


def test_sheet_source_is_only_required_by_the_sync_job_so_other_services_still_start(
    engine: Engine,
) -> None:
    import os
    import subprocess
    import sys

    from donation_ledger.config import ConfigError
    from donation_ledger.sources import SheetReadError, make_reader
    from tests.conftest import database_url_for_tests

    base = {"DATABASE_URL": "postgresql://h/d"}
    cfg = Settings.from_env(base)  # web service / reconcile / admin must load without it
    with pytest.raises(SheetReadError):
        make_reader(cfg)  # ... but the sync must never fall back to the sample CSV
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "SHEET_SOURCE": "excel"})
    assert Settings.from_env({**base, "SHEET_SOURCE": "CSV"}).sheet_source == "csv"
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "RECONCILE_LOOKBACK_HOURS": "10000000000"})
    env = {k: v for k, v in os.environ.items() if k != "SHEET_SOURCE"}
    env["DATABASE_URL"] = database_url_for_tests()
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "donation_ledger.sync"], env=env, capture_output=True
    )
    assert proc.returncode == 1
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    web = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "from donation_ledger.main import create; create()"],
        env=env,
        capture_output=True,
    )
    assert web.returncode == 0, web.stderr.decode()[-300:]


def test_implausible_stripe_created_dates_are_quarantined_and_api_survives_any_session_timezone(
    engine: Engine, settings: Settings
) -> None:
    from tests.conftest import FAKE_API_TOKEN

    c = TestClient(create_app(settings, engine))
    for pid, created in (("pi_Y1", -62135596800), ("pi_Y9999", 253402300799)):
        body = encode(event(payment_intent(pid, created=created)))
        hdr = {"Stripe-Signature": sign(body, FAKE_WEBHOOK_SECRET)}
        assert c.post("/webhooks/stripe", content=body, headers=hdr).status_code == 200
    assert rows(engine, "SELECT count(*) FROM donations") == [(0,)]
    assert rows(engine, "SELECT count(*) FROM rejected_rows") == [(2,)]
    with engine.connect() as conn:
        assert conn.execute(text("SHOW timezone")).scalar_one() == "UTC"
    assert (
        c.get("/donations", headers={"Authorization": f"Bearer {FAKE_API_TOKEN}"}).status_code
        == 200
    )


def test_stale_bad_snapshot_of_an_existing_payment_is_a_plain_duplicate_and_does_not_reopen_anything(
    engine: Engine, settings: Settings
) -> None:
    c = TestClient(create_app(settings, engine))

    def deliver(obj: dict[str, Any], evt: str) -> Any:
        body = encode(event(obj, event_id=evt))
        hdr = {"Stripe-Signature": sign(body, FAKE_WEBHOOK_SECRET)}
        return c.post("/webhooks/stripe", content=body, headers=hdr).json()

    bad = payment_intent("pi_STALE")
    bad["amount_received"] = 0
    assert deliver(bad, "e1") == {"status": "quarantined"}
    assert deliver(payment_intent("pi_STALE"), "e2") == {"status": "created"}
    with engine.begin() as conn:
        conn.execute(text("UPDATE rejected_rows SET status='resolved'"))
    assert deliver(bad, "e1") == {"status": "duplicate"}
    assert rows(engine, "SELECT status FROM rejected_rows") == [("resolved",)]


def test_client_library_errors_are_stored_as_type_only_so_key_fragments_never_leak() -> None:
    import stripe

    from donation_ledger.runs import safe_error_message

    err = stripe.AuthenticationError("Invalid API Key provided: sk_test_****abCd")
    assert safe_error_message(err) == "AuthenticationError"


def test_overlong_stripe_email_is_dropped_not_stored() -> None:
    result = map_payment_intent(payment_intent(receipt_email="a" * 900_000 + "@x.org"))
    assert isinstance(result, NewDonation) and result.donor_email is None


def test_stripe_anonymous_wrapped_in_invisible_characters_is_still_anonymous() -> None:
    for name in ("Anonymous\u200b", "\u200bANONYMOUS", "\ufeffanonymous"):
        result = map_payment_intent(payment_intent(metadata={"donor_name": name}))
        assert isinstance(result, NewDonation) and result.donor_name is None
        assert "nonymous" not in str(result.raw_payload).lower()


def test_zero_decimal_currency_accepts_trailing_zero_fraction_but_not_real_fractions() -> None:
    from donation_ledger.parsing import RowError, minor_units_from_decimal_string

    assert minor_units_from_decimal_string("5000.00", "JPY") == 5000
    assert minor_units_from_decimal_string("$1,000.0", "USD") == 100000
    for bad in ("5000.50", "1.005"):
        with pytest.raises(RowError):
            minor_units_from_decimal_string(bad, "JPY" if bad == "5000.50" else "USD")


def test_bare_pi_prefix_is_not_a_valid_payment_intent_id() -> None:
    assert isinstance(map_payment_intent(payment_intent("pi_")), Rejection)
    assert isinstance(map_payment_intent(payment_intent("pi_\x00")), Rejection)


def test_run_whose_lock_was_lost_cannot_overwrite_a_newer_runs_bookkeeping(engine: Engine) -> None:
    from sqlalchemy.orm import Session

    from donation_ledger.runs import assert_run_still_ours, fail_stale_runs, start_run

    with Session(engine) as s, s.begin():
        old = start_run(s, "sheet")
    with Session(engine) as s, s.begin():
        fail_stale_runs(s, "sheet")  # what a newer lock holder does to the old run
    with pytest.raises(RuntimeError), Session(engine) as s, s.begin():
        assert_run_still_ours(s, old)
    assert rows(engine, "SELECT status FROM sync_runs") == [("failed",)]


def test_reconcile_does_not_reopen_a_rejection_for_a_payment_already_in_the_ledger(
    engine: Engine,
) -> None:
    from datetime import timedelta

    from donation_ledger.reconcile import run_reconcile

    class Source:
        def __init__(self, items: list[dict[str, Any]]) -> None:
            self.items = items

        def list_since(self, created_gte: Any) -> Any:
            return self.items

    run_reconcile(engine, Source([payment_intent("pi_G")]), timedelta(hours=1))
    bad = payment_intent("pi_G")
    bad["amount_received"] = 0
    res = run_reconcile(engine, Source([bad]), timedelta(hours=1))
    assert res.counters.rows_quarantined == 0
    assert rows(engine, "SELECT count(*) FROM rejected_rows") == [(0,)]


def test_identical_bad_rows_get_separate_rejection_records_so_ignoring_one_keeps_the_other(
    engine: Engine,
) -> None:
    from donation_ledger.sync import run_sync
    from tests.test_sync import TZ, ListReader, good

    cash = good("", "20", "Cash Donor")
    run_sync(engine, ListReader([cash, list(cash)]), TZ)
    assert rows(engine, "SELECT count(*) FROM rejected_rows") == [(2,)]
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE rejected_rows SET status='ignored' WHERE id=(SELECT min(id) FROM rejected_rows)"
            )
        )
    res = run_sync(engine, ListReader([cash, list(cash)]), TZ)
    assert (res.counters.rows_ignored, res.counters.rows_quarantined) == (1, 1)


def test_settings_repr_never_contains_secrets() -> None:
    cfg = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://u:dbpass@h/d",
            "API_TOKEN": "tok_secret",
            "STRIPE_WEBHOOK_SECRET": "whsec_real",
            "STRIPE_API_KEY": "sk_test_abc",
            "GOOGLE_SERVICE_ACCOUNT_JSON": "{private}",
        }
    )
    text_ = repr(cfg)
    for secret in ("dbpass", "tok_secret", "whsec_real", "sk_test_abc", "{private}"):
        assert secret not in text_


def test_admin_listing_escapes_control_characters_from_entry_ids() -> None:
    from donation_ledger.admin import _printable

    assert _printable("a\x1b[31m\nb") == "a\\x1b[31m\\nb"


def test_client_secret_is_never_stored_in_payloads_or_quarantine(engine: Engine) -> None:
    from sqlalchemy.orm import sessionmaker

    from donation_ledger.webhook import process_event

    sf = sessionmaker(engine)
    good_pi = payment_intent(
        "pi_CS1", client_secret="pi_cs1_secret_FAKEFAKE", nested={"client_secret": "x"}
    )
    bad_pi = payment_intent("pi_CS2", amount=-1, client_secret="pi_cs2_secret_FAKEFAKE")
    bad_pi["amount_received"] = -1
    process_event(sf, event(good_pi))
    process_event(sf, event(bad_pi, event_id="evt_2"))
    dump = rows(
        engine,
        "SELECT (SELECT string_agg(raw_payload::text,'') FROM donations) || "
        "(SELECT string_agg(raw_row::text,'') FROM rejected_rows)",
    )[0][0]
    assert "secret" not in str(dump).lower()


def test_env_example_placeholders_are_rejected_as_real_secrets() -> None:
    from donation_ledger.config import ConfigError

    for name, value in (
        ("STRIPE_WEBHOOK_SECRET", "whsec_replace_me"),
        ("API_TOKEN", "replace-me"),
        ("STRIPE_API_KEY", "sk_test_replace_me"),
    ):
        with pytest.raises(ConfigError):
            Settings.from_env({"DATABASE_URL": "postgresql://h/d", name: value})
