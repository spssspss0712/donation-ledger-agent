"""GET /followups: donations whose donor_email is NULL (SPEC 2.3 "Follow-up list")."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from donation_ledger.api import create_app
from donation_ledger.config import Settings
from donation_ledger.sync import run_sync
from tests.conftest import FAKE_API_TOKEN
from tests.test_sync import TZ, ListReader
from tests.test_sync_contacts import row

AUTH = {"Authorization": f"Bearer {FAKE_API_TOKEN}"}


@pytest.fixture
def client(engine: Engine, settings: Settings) -> TestClient:
    return TestClient(create_app(settings, engine))


def seed(engine: Engine) -> None:
    run_sync(
        engine,
        ListReader(
            [
                row("F1", name="Has Email", email="has@example.org", address="9 Secret Rd"),
                row("F2", name="No Email", address="8 Hidden St"),
                row("F3", email="not-valid", address="7 Private Ln"),
            ]
        ),
        TZ,
        500,
    )


def test_followups_lists_only_donations_without_an_email_with_the_reason(
    client: TestClient, engine: Engine
) -> None:
    seed(engine)
    items = client.get("/followups", headers=AUTH).json()["items"]
    assert [(i["external_id"], i["reason"]) for i in items] == [
        ("F3", "invalid_email"),
        ("F2", "missing_email"),
    ]
    assert set(items[0]) == {"donation_id", "source", "external_id", "date", "reason"}
    assert items[0]["source"] == "sheet" and items[0]["date"].startswith("2024-03-01")


def test_followups_never_returns_an_email_address_or_a_postal_address(
    client: TestClient, engine: Engine
) -> None:
    seed(engine)
    body = client.get("/followups", headers=AUTH).text
    for secret in (
        "has@example.org",
        "not-valid",
        "Secret Rd",
        "Hidden St",
        "Private Ln",
        "No Email",
    ):
        assert secret not in body


def test_a_donation_leaves_the_followup_list_once_its_email_is_filled_in(
    client: TestClient, engine: Engine
) -> None:
    seed(engine)
    run_sync(
        engine,
        ListReader([row("F2", name="No Email", email="now@example.org", address="8 Hidden St")]),
        TZ,
        500,
    )
    ids = [i["external_id"] for i in client.get("/followups", headers=AUTH).json()["items"]]
    assert ids == ["F3"]


def test_followups_requires_the_token_and_fails_closed(
    client: TestClient, engine: Engine, settings: Settings
) -> None:
    assert client.get("/followups").status_code == 401
    assert client.get("/followups", headers={"Authorization": "Bearer nope"}).status_code == 401
    closed = TestClient(create_app(replace(settings, api_token=None), engine))
    assert closed.get("/followups").status_code == 503


def test_followups_are_paginated_without_gaps_or_repeats_and_filterable_by_source(
    client: TestClient, engine: Engine
) -> None:
    run_sync(engine, ListReader([row(f"G{i}") for i in range(7)]), TZ, 500)
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params: dict[str, object] = {"limit": 3}
        if cursor:
            params["before_id"] = cursor
        page = client.get("/followups", params=params, headers=AUTH).json()
        assert len(page["items"]) <= 3
        seen += [i["external_id"] for i in page["items"]]
        cursor = page["next_before_id"]
        if cursor is None:
            break
    assert sorted(seen) == [f"G{i}" for i in range(7)]
    assert client.get("/followups", params={"source": "stripe"}, headers=AUTH).json()["items"] == []
    assert client.get("/followups", params={"limit": 0}, headers=AUTH).status_code == 422
    assert client.get("/followups", params={"limit": 201}, headers=AUTH).status_code == 422
    assert client.get("/followups", params={"source": "x"}, headers=AUTH).status_code == 422


def test_stripe_donations_without_email_are_listed_too(client: TestClient, engine: Engine) -> None:
    from donation_ledger.db import make_session_factory
    from donation_ledger.webhook import process_event
    from tests.helpers import event, payment_intent

    sf = make_session_factory(engine)
    process_event(sf, event(payment_intent("pi_NOMAIL")))
    process_event(sf, event(payment_intent("pi_BAD", receipt_email="x")))
    process_event(sf, event(payment_intent("pi_OK", receipt_email="x@y.org")))
    items = client.get("/followups", headers=AUTH).json()["items"]
    assert sorted((i["external_id"], i["reason"], i["source"]) for i in items) == [
        ("pi_BAD", "invalid_email", "stripe"),
        ("pi_NOMAIL", "missing_email", "stripe"),
    ]
