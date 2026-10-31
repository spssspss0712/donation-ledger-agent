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

    rng = random.Random(1234)  # fixed seed: reproducible
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
