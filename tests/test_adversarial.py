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
