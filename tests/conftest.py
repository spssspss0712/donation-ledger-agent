"""Shared fixtures. Tests run against a REAL PostgreSQL (TEST_DATABASE_URL)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from donation_ledger.config import Settings, normalize_database_url
from donation_ledger.db import make_engine, make_session_factory

DEFAULT_TEST_URL = "postgresql+psycopg://ledger:ledger@localhost:5432/ledger_test"

# Clearly fake credentials, only used by tests.
FAKE_WEBHOOK_SECRET = "whsec_FAKE_test_secret_not_real"  # noqa: S105
FAKE_API_TOKEN = "FAKE-api-token-for-tests"  # noqa: S105


def database_url_for_tests() -> str:
    return normalize_database_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL))


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = database_url_for_tests()
    eng = make_engine(url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config("alembic.ini")
    os.environ["DATABASE_URL"] = url
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_db(engine: Engine) -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE donations, rejected_rows, sync_issues, sync_runs RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=database_url_for_tests(),
        stripe_webhook_secret=FAKE_WEBHOOK_SECRET,
        api_token=FAKE_API_TOKEN,
        sync_batch_size=500,
    )
