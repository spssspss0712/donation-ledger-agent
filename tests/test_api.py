from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from donation_ledger.admin import list_rejected, set_status
from donation_ledger.api import create_app
from donation_ledger.config import ConfigError, Settings, normalize_database_url
from donation_ledger.logging_setup import JsonFormatter
from donation_ledger.sources import CsvSheetReader
from donation_ledger.sources.google_reader import GoogleSheetReader
from donation_ledger.sync import run_sync
from tests.conftest import FAKE_API_TOKEN
from tests.test_sync import SAMPLE, TZ, ListReader, good, rows

AUTH = {"Authorization": f"Bearer {FAKE_API_TOKEN}"}


@pytest.fixture
def client(engine: Engine, settings: Settings) -> TestClient:
    return TestClient(create_app(settings, engine))


def seed(engine: Engine, n: int = 5) -> None:
    run_sync(
        engine,
        ListReader([good(f"A{i}", name="" if i == 0 else f"Donor {i}") for i in range(n)]),
        TZ,
        500,
    )


def test_health_reports_ok_when_database_is_reachable(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200 and resp.json()["db"] == "ok"


def test_health_reports_503_when_database_is_unreachable(settings: Settings) -> None:
    from donation_ledger.db import make_engine

    dead = make_engine("postgresql+psycopg://x:y@127.0.0.1:1/none?connect_timeout=1")
    assert TestClient(create_app(settings, dead)).get("/health").status_code == 503


def test_donations_requires_the_bearer_token(client: TestClient) -> None:
    assert client.get("/donations").status_code == 401
    assert client.get("/donations", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/sync-runs").status_code == 401
    assert client.get("/donations", headers=AUTH).status_code == 200


def test_api_fails_closed_when_no_token_is_configured(engine: Engine, settings: Settings) -> None:
    c = TestClient(create_app(replace(settings, api_token=None), engine))
    assert c.get("/donations").status_code == 503


def test_anonymous_donors_are_shown_as_anonymous_while_storage_stays_null(
    client: TestClient, engine: Engine
) -> None:
    seed(engine)
    items = client.get("/donations", headers=AUTH).json()["items"]
    anon = [i for i in items if i["external_id"] == "A0"][0]
    assert anon["donor_name"] == "Anonymous" and anon["is_anonymous"] is True
    assert rows(engine, "SELECT donor_name FROM donations WHERE external_id='A0'") == [(None,)]


def test_donations_endpoint_never_exposes_email_address_or_raw_payload(
    client: TestClient, engine: Engine
) -> None:
    seed(engine, 2)
    item = client.get("/donations", headers=AUTH).json()["items"][0]
    assert set(item) == {
        "id",
        "source",
        "external_id",
        "donor_name",
        "is_anonymous",
        "recipient",
        "amount_minor",
        "currency",
        "donated_at",
        "created_at",
    }


def test_donations_are_paginated_without_gaps_or_repeats(
    client: TestClient, engine: Engine
) -> None:
    seed(engine, 7)
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params: dict[str, object] = {"limit": 3}
        if cursor:
            params["before_id"] = cursor
        page = client.get("/donations", params=params, headers=AUTH).json()
        seen += [i["external_id"] for i in page["items"]]
        cursor = page["next_before_id"]
        if cursor is None:
            break
    assert sorted(seen) == [f"A{i}" for i in range(7)] and len(seen) == 7


def test_donations_can_be_filtered_by_source(client: TestClient, engine: Engine) -> None:
    seed(engine, 3)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO donations (source, external_id, amount_minor, currency, donated_at, "
                "is_anonymous, raw_payload) VALUES ('stripe','pi_1',5,'USD',now(),true,'{}')"
            )
        )
    sheet = client.get("/donations", params={"source": "sheet"}, headers=AUTH).json()["items"]
    stripe = client.get("/donations", params={"source": "stripe"}, headers=AUTH).json()["items"]
    assert len(sheet) == 3 and all(i["source"] == "sheet" for i in sheet)
    assert [i["external_id"] for i in stripe] == ["pi_1"]
    assert client.get("/donations", params={"source": "paypal"}, headers=AUTH).status_code == 422


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1000}, {"before_id": 0}])
def test_invalid_pagination_parameters_are_rejected(
    client: TestClient, params: dict[str, int]
) -> None:
    assert client.get("/donations", params=params, headers=AUTH).status_code == 422


def test_sync_runs_endpoint_lists_latest_first_with_counters(
    client: TestClient, engine: Engine
) -> None:
    run_sync(engine, CsvSheetReader(SAMPLE), TZ, 500)
    run_sync(engine, ListReader([]), TZ, 500)
    runs = client.get("/sync-runs", headers=AUTH).json()
    assert [r["id"] for r in runs] == [2, 1]
    assert runs[1]["rows_inserted"] == 6 and runs[1]["rows_quarantined"] == 9
    assert runs[1]["status"] == "succeeded" and runs[0]["rows_read"] == 0


def test_admin_can_ignore_and_reopen_a_rejected_row(engine: Engine) -> None:
    run_sync(engine, ListReader([good("Z1", "bad")]), TZ, 500)
    (rej,) = list_rejected(engine)
    assert set_status(engine, rej.id, "ignored") is True
    assert list_rejected(engine) == []
    assert set_status(engine, rej.id, "open") is True
    assert set_status(engine, 9999, "ignored") is False


def test_error_messages_stored_for_database_failures_contain_no_donor_data(engine: Engine) -> None:
    from sqlalchemy import event

    def boom(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("INSERT INTO DONATIONS"):
            cursor.execute("SELECT 1/0")

    event.listen(engine, "before_cursor_execute", boom)
    try:
        run_sync(engine, ListReader([good("PII1", name="Secret Person Name")]), TZ, 500)
    finally:
        event.remove(engine, "before_cursor_execute", boom)
    message = rows(engine, "SELECT error_message FROM sync_runs")[0][0]
    assert "Secret Person Name" not in str(message) and "division by zero" in str(message)


def test_json_log_lines_are_valid_json_and_omit_tracebacks_with_parameters() -> None:
    try:
        raise ValueError("Secret Person Name")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, "f", 1, "failed", (), sys.exc_info())
    line = JsonFormatter().format(record)
    parsed = json.loads(line)
    assert parsed["message"] == "failed" and parsed["exc_type"] == "ValueError"
    assert "Secret Person Name" not in line


def test_webhook_and_sync_logs_do_not_contain_donor_pii(
    client: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.conftest import FAKE_WEBHOOK_SECRET
    from tests.helpers import encode, event, payment_intent, sign

    caplog.set_level(logging.DEBUG)
    obj = payment_intent(
        receipt_email="private.person@example.org",
        metadata={"donor_name": "Private Person"},
    )
    body = encode(event(obj))
    client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sign(body, FAKE_WEBHOOK_SECRET)},
    )
    run_sync(engine, ListReader([good("LOG1", name="Private Sheet Person", amount="bad")]), TZ, 500)
    logged = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    for secret in (
        "private.person@example.org",
        "Private Person",
        "Private Sheet Person",
        FAKE_WEBHOOK_SECRET,
    ):
        assert secret not in logged
    assert logged  # we did log something


def test_config_refuses_live_stripe_keys_and_bad_timezones() -> None:
    base = {"DATABASE_URL": "postgres://u:p@h/db", "SHEET_SOURCE": "csv"}
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "STRIPE_API_KEY": "sk_live_FAKE"})
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "SHEET_TIMEZONE": "Mars/Base"})
    with pytest.raises(ConfigError):
        Settings.from_env({})
    ok = Settings.from_env({**base, "STRIPE_API_KEY": "sk_test_FAKE"})
    assert ok.database_url.startswith("postgresql+psycopg://")
    assert normalize_database_url("postgresql://a/b") == "postgresql+psycopg://a/b"


def test_google_reader_reports_failures_without_leaking_credentials() -> None:
    from donation_ledger.sources import SheetReadError

    reader = GoogleSheetReader("sheet-id", None, '{"private_key": "FAKE-KEY-MATERIAL"}')
    with pytest.raises(SheetReadError) as info:
        reader.read()
    assert "FAKE-KEY-MATERIAL" not in str(info.value)
