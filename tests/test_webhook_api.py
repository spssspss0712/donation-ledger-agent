from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from donation_ledger.api import create_app
from donation_ledger.config import Settings
from donation_ledger.webhook import Outcome, process_event
from tests.conftest import FAKE_WEBHOOK_SECRET
from tests.helpers import encode, event, payment_intent, sign


@pytest.fixture
def client(engine: Engine, settings: Settings) -> TestClient:
    return TestClient(create_app(settings, engine), raise_server_exceptions=False)


def post(client: TestClient, body: bytes, header: str | None) -> Any:
    headers = {"Content-Type": "application/json"}
    if header is not None:
        headers["Stripe-Signature"] = header
    return client.post("/webhooks/stripe", content=body, headers=headers)


def count(engine: Engine, table: str = "donations") -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def test_validly_signed_payment_event_creates_exactly_one_donation(
    client: TestClient, engine: Engine
) -> None:
    body = encode(event(payment_intent(receipt_email="Ada@Example.org")))
    resp = post(client, body, sign(body, FAKE_WEBHOOK_SECRET))
    assert resp.status_code == 200
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT source, external_id, amount_minor, currency, donor_name, "
                "is_anonymous, donor_email, donated_at FROM donations"
            )
        ).one()
    assert row.source == "stripe"
    assert row.external_id == "pi_FAKE0001"
    assert row.amount_minor == 2500
    assert row.currency == "USD"
    assert row.donor_name is None and row.is_anonymous is True
    assert row.donor_email == "ada@example.org"
    assert row.donated_at.isoformat() == "2024-12-24T00:26:40+00:00"


def test_same_event_delivered_twice_stores_one_record_and_still_returns_2xx(
    client: TestClient, engine: Engine
) -> None:
    body = encode(event())
    assert post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 200
    assert post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 200
    assert count(engine) == 1


def test_different_events_for_the_same_payment_intent_store_one_record(
    client: TestClient, engine: Engine
) -> None:
    for evt_id in ("evt_A", "evt_B", "evt_C"):
        body = encode(event(event_id=evt_id))
        assert post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 200
    assert count(engine) == 1


def test_wrong_signature_returns_400_and_writes_nothing(client: TestClient, engine: Engine) -> None:
    body = encode(event())
    resp = post(client, body, sign(body, "whsec_FAKE_some_other_secret"))
    assert resp.status_code == 400
    assert count(engine) == 0 and count(engine, "rejected_rows") == 0


def test_missing_signature_returns_400_and_writes_nothing(
    client: TestClient, engine: Engine
) -> None:
    assert post(client, encode(event()), None).status_code == 400
    assert count(engine) == 0


def test_malformed_signature_header_returns_400(client: TestClient, engine: Engine) -> None:
    for header in ("garbage", "t=abc,v1=zz", "t=1,v1=", ""):
        assert post(client, encode(event()), header).status_code == 400
    assert count(engine) == 0


def test_body_changed_after_signing_is_rejected(client: TestClient, engine: Engine) -> None:
    body = encode(event())
    header = sign(body, FAKE_WEBHOOK_SECRET)
    tampered = body.replace(b"2500", b"9900")
    assert post(client, tampered, header).status_code == 400
    assert count(engine) == 0


def test_signature_is_checked_against_raw_bytes_not_reserialised_json(
    client: TestClient, engine: Engine
) -> None:
    # Same JSON meaning, different bytes (pretty-printed). Signed as-is, it must pass;
    # a signature computed over the compact form must fail.
    evt = event()
    pretty = json.dumps(evt, indent=4).encode()
    compact = encode(evt)
    assert post(client, pretty, sign(pretty, FAKE_WEBHOOK_SECRET)).status_code == 200
    assert post(client, pretty, sign(compact, FAKE_WEBHOOK_SECRET)).status_code == 400


def test_old_timestamp_replay_is_rejected(client: TestClient, engine: Engine) -> None:
    body = encode(event())
    old = sign(body, FAKE_WEBHOOK_SECRET, timestamp=1_600_000_000)
    assert post(client, body, old).status_code == 400
    assert count(engine) == 0


def test_signed_non_json_body_returns_400(client: TestClient) -> None:
    body = b"not json at all"
    assert post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 400


def test_unrecognised_event_type_returns_2xx_and_writes_to_no_table(
    client: TestClient, engine: Engine
) -> None:
    for type_ in (
        "charge.refunded",
        "charge.dispute.created",
        "payment_intent.created",
        "payment_intent.payment_failed",
        "totally.unknown",
    ):
        body = encode(event(type_=type_))
        assert post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 200
    for table in ("donations", "rejected_rows", "sync_issues", "sync_runs"):
        assert count(engine, table) == 0


@pytest.mark.parametrize(
    "obj",
    [
        payment_intent(amount=0),
        payment_intent(amount=-5),
        payment_intent(pi_id="ch_notapi"),
        payment_intent(currency=""),
        {"id": "pi_X", "currency": "usd", "created": 1},
    ],
)
def test_signed_but_unusable_payment_is_quarantined_not_dropped_or_retried_forever(
    client: TestClient, engine: Engine, obj: dict[str, Any]
) -> None:
    obj = dict(obj)
    if obj.get("amount") in (0, -5):
        obj["amount_received"] = obj["amount"]
    body = encode(event(obj))
    assert post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 200
    assert count(engine) == 0
    assert count(engine, "rejected_rows") == 1


def test_event_without_data_object_is_quarantined(client: TestClient, engine: Engine) -> None:
    body = encode({"id": "evt_1", "type": "payment_intent.succeeded", "data": {}})
    assert post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 200
    assert count(engine, "rejected_rows") == 1


def test_nul_and_lone_surrogate_in_payload_do_not_break_storage(
    client: TestClient, engine: Engine
) -> None:
    obj = payment_intent(metadata={"donor_name": "Bad\u0000Name\ud800"})
    body = json.dumps(event(obj)).encode()  # ensure_ascii escapes -> valid JSON text
    assert post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 200
    with engine.connect() as conn:
        name = conn.execute(text("SELECT donor_name FROM donations")).scalar_one()
    assert "\x00" not in name


def test_name_and_email_are_read_from_metadata_by_the_webhook_end_to_end(
    client: TestClient, engine: Engine
) -> None:
    obj = payment_intent(metadata={"donor_name": " Grace  Hopper ", "donor_email": "G@X.org"})
    body = encode(event(obj))
    assert post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 200
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT donor_name, donor_email, is_anonymous FROM donations")
        ).one()
    assert (row.donor_name, row.donor_email, row.is_anonymous) == ("Grace Hopper", "g@x.org", False)


def test_amount_received_is_stored_when_it_differs_from_amount(
    client: TestClient, engine: Engine
) -> None:
    obj = payment_intent(amount=5000)
    obj["amount_received"] = 3000
    body = encode(event(obj))
    post(client, body, sign(body, FAKE_WEBHOOK_SECRET))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT amount_minor FROM donations")).scalar_one() == 3000


def test_database_failure_returns_5xx_so_stripe_retries_and_a_retry_then_succeeds(
    client: TestClient, engine: Engine
) -> None:
    body = encode(event())
    header = sign(body, FAKE_WEBHOOK_SECRET)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE donations RENAME TO donations_broken"))
    try:
        resp = post(client, body, header)
        assert resp.status_code == 500
    finally:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE donations_broken RENAME TO donations"))
    assert post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 200
    assert count(engine) == 1


def test_oversized_body_is_refused_without_writing(client: TestClient, engine: Engine) -> None:
    body = b"x" * 1_100_000
    resp = post(client, body, sign(body, FAKE_WEBHOOK_SECRET))
    assert resp.status_code == 413
    assert count(engine) == 0


def test_webhook_refuses_everything_when_no_secret_is_configured(
    engine: Engine, settings: Settings
) -> None:
    from dataclasses import replace

    c = TestClient(create_app(replace(settings, stripe_webhook_secret=None), engine))
    body = encode(event())
    assert post(c, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code == 503
    assert count(engine) == 0


def test_error_responses_do_not_leak_secrets_or_payload(client: TestClient) -> None:
    body = encode(event(payment_intent(receipt_email="leak@example.org")))
    resp = post(client, body, sign(body, "whsec_FAKE_wrong"))
    assert "leak@example.org" not in resp.text and FAKE_WEBHOOK_SECRET not in resp.text


def test_concurrent_deliveries_of_one_event_store_exactly_one_record(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    evt = event(payment_intent(pi_id="pi_FAKE_RACE"))
    n = 16
    barrier = threading.Barrier(n)
    outcomes: list[Outcome] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait()
            result = process_event(session_factory, evt)
            with lock:
                outcomes.append(result)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert count(engine) == 1
    assert outcomes.count(Outcome.CREATED) == 1
    assert outcomes.count(Outcome.DUPLICATE) == n - 1


def test_concurrent_deliveries_through_http_store_exactly_one_record(
    client: TestClient, engine: Engine
) -> None:
    body = encode(event(payment_intent(pi_id="pi_FAKE_HTTP_RACE")))
    statuses: list[int] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        statuses.append(post(client, body, sign(body, FAKE_WEBHOOK_SECRET)).status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses == [200] * 8
    assert count(engine) == 1
