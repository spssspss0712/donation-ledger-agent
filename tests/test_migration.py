from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, exc, inspect, text
from sqlalchemy.engine import make_url

from donation_ledger.db import make_engine
from donation_ledger.models import Base
from tests.conftest import database_url_for_tests


def test_migration_creates_all_five_required_tables(engine: Engine) -> None:
    names = set(inspect(engine).get_table_names())
    assert {
        "donations",
        "sync_runs",
        "rejected_rows",
        "sync_issues",
        "donation_contact_changes",
    } <= names


def test_models_and_alembic_migration_describe_the_same_schema(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def _insert_donation(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO donations (source, external_id, amount_minor, currency, "
                    "donated_at, is_anonymous, raw_payload) "
                    "VALUES ('sheet', 'M-1', 1000, 'USD', '2024-01-01T00:00:00Z', true, '{}') "
                    "RETURNING id"
                )
            ).scalar_one()
        )


@pytest.mark.parametrize(
    "assignment",
    [
        "amount_minor = amount_minor + 1",
        "currency = 'EUR'",
        "donated_at = donated_at + interval '1 second'",
        "source = 'stripe'",
        "external_id = 'M-2'",
    ],
)
def test_database_rejects_direct_sql_update_of_each_money_field(
    engine: Engine, assignment: str
) -> None:
    donation_id = _insert_donation(engine)
    with pytest.raises(exc.DBAPIError, match="immutable"), engine.begin() as conn:
        conn.execute(text(f"UPDATE donations SET {assignment} WHERE id = :i"), {"i": donation_id})
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT source, external_id, amount_minor, currency FROM donations")
        ).one()
    assert tuple(row) == ("sheet", "M-1", 1000, "USD")


def test_database_rejects_a_bulk_update_that_touches_money_even_if_one_row_is_fine(
    engine: Engine,
) -> None:
    _insert_donation(engine)
    with pytest.raises(exc.DBAPIError, match="immutable"), engine.begin() as conn:
        # Setting a money column to its current value is harmless; changing it is not.
        conn.execute(text("UPDATE donations SET amount_minor = 1000, currency = 'GBP'"))


def test_database_allows_money_columns_to_be_rewritten_with_the_same_values(engine: Engine) -> None:
    donation_id = _insert_donation(engine)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE donations SET amount_minor = 1000, currency = 'USD' WHERE id = :i"),
            {"i": donation_id},
        )


def test_database_allows_contact_fields_to_be_updated(engine: Engine) -> None:
    donation_id = _insert_donation(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE donations SET donor_email = 'a@b.org', donor_name = 'Ann', "
                "is_anonymous = false WHERE id = :i"
            ),
            {"i": donation_id},
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT donor_email FROM donations")).scalar_one() == "a@b.org"


# --- upgrading a database that already holds version 1 data -------------------------------


@pytest.fixture
def scratch_url() -> Iterator[str]:
    """A throw-away database, so the shared test database is never downgraded."""
    base = make_url(database_url_for_tests())
    name = "ledger_test_upgrade_scratch"
    admin = create_engine(base.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        conn.execute(text(f"CREATE DATABASE {name}"))
    yield base.set(database=name).render_as_string(hide_password=False)
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    admin.dispose()


def _alembic(url: str, target: str) -> None:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config("alembic.ini"), target)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_upgrading_a_populated_version_1_database_keeps_every_row_and_leaves_email_null(
    scratch_url: str,
) -> None:
    _alembic(scratch_url, "0001")
    eng = make_engine(scratch_url)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sync_runs (id, source, status, rows_read, rows_inserted) "
                "VALUES (7, 'sheet', 'succeeded', 3, 2)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO donations (id, source, external_id, donor_name, donor_address, "
                "recipient, amount_minor, currency, donated_at, is_anonymous, raw_payload) VALUES "
                "(1, 'sheet', 'V1-A', 'Ann', '1 Road', 'Fund', 1050, 'USD', "
                '\'2024-03-01T08:00:00Z\', false, \'{"fields": {"entry_id": "V1-A"}}\'), '
                "(2, 'stripe', 'pi_V1', NULL, NULL, NULL, 2500, 'USD', "
                "'2024-04-01T00:00:00Z', true, '{\"id\": \"pi_V1\"}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO rejected_rows (source, external_id, raw_row, reason, row_fingerprint, "
                "sync_run_id) VALUES ('sheet', 'V1-B', '{}', 'invalid_amount: x', 'fp1', 7)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO sync_issues (source, external_id, kind, detail, detail_hash) "
                "VALUES ('sheet', 'V1-A', 'changed_after_sync', '{}', 'h1')"
            )
        )
        before = [tuple(r) for r in conn.execute(text("SELECT * FROM donations ORDER BY id"))]
    _alembic(scratch_url, "head")
    with eng.connect() as conn:
        after_cols = list(conn.execute(text("SELECT * FROM donations ORDER BY id")).keys())
        after = [tuple(r) for r in conn.execute(text("SELECT * FROM donations ORDER BY id"))]
        emails = conn.execute(text("SELECT donor_email FROM donations ORDER BY id")).all()
        run = conn.execute(text("SELECT status, rows_read, contacts_filled FROM sync_runs")).one()
        counts = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM rejected_rows), (SELECT count(*) FROM sync_issues), "
                "(SELECT count(*) FROM donation_contact_changes)"
            )
        ).one()
    assert "donor_email" in after_cols
    assert after == before  # untouched, byte for byte (donor_email was already a v1 column)
    assert [e[0] for e in emails] == [None, None]
    assert tuple(run) == ("succeeded", 3, 0)
    assert tuple(counts) == (1, 1, 0)
    # The trigger exists on the upgraded database as well.
    with pytest.raises(exc.DBAPIError, match="immutable"), eng.begin() as conn:
        conn.execute(text("UPDATE donations SET amount_minor = 1 WHERE id = 1"))
    eng.dispose()


def test_downgrade_removes_trigger_audit_table_and_counter_column_and_keeps_donations(
    scratch_url: str,
) -> None:
    _alembic(scratch_url, "head")
    eng = make_engine(scratch_url)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO donations (source, external_id, amount_minor, currency, donated_at, "
                "is_anonymous, raw_payload) VALUES ('sheet','D-1',5,'USD',now(),true,'{}')"
            )
        )
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = scratch_url
    try:
        command.downgrade(Config("alembic.ini"), "0001")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    insp = inspect(eng)
    assert "donation_contact_changes" not in insp.get_table_names()
    assert "contacts_filled" not in {c["name"] for c in insp.get_columns("sync_runs")}
    with eng.begin() as conn:
        conn.execute(text("UPDATE donations SET amount_minor = 6"))  # trigger gone
        assert conn.execute(text("SELECT count(*) FROM donations")).scalar_one() == 1
    eng.dispose()


def test_version_2_sync_on_an_upgraded_version_1_database_fills_contacts_without_false_warnings(
    scratch_url: str,
) -> None:
    from donation_ledger.sync import run_sync
    from tests.test_sync import ListReader

    _alembic(scratch_url, "0001")
    eng = make_engine(scratch_url)
    with eng.begin() as conn:
        # Exactly what version 1 stored for a sheet row: 7 field keys, no donor_email key.
        conn.execute(
            text(
                "INSERT INTO donations (source, external_id, donor_name, donor_address, recipient,"
                " amount_minor, currency, donated_at, is_anonymous, raw_payload) VALUES "
                "('sheet', 'V1-A', NULL, '1 Road', 'Fund', 1000, 'USD', '2024-03-01T08:00:00Z',"
                ' true, \'{"fields": {"entry_id": "V1-A", "date": "2024-03-01",'
                ' "donor_name": "", "donor_address": "1 Road", "amount": "10",'
                ' "currency": "USD", "recipient": "Fund"}, "extra": {}, "sheet_row": 2}\')'
            )
        )
    _alembic(scratch_url, "head")
    header = [
        "Entry ID", "Date", "Donor Name", "Donor Email", "Donor Address", "Amount", "Currency",
        "Recipient",
    ]  # fmt: skip
    sheet = ListReader(
        [["V1-A", "2024-03-01", "Ann", "Ann@Example.org", "1 Road", "10", "USD", "Fund"]],
        header=header,
    )
    res = run_sync(eng, sheet, "America/Los_Angeles", 500)
    assert res.status == "succeeded" and res.counters.contacts_filled == 2
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT donor_name, is_anonymous, donor_email, amount_minor FROM donations")
        ).one()
        issues = conn.execute(text("SELECT count(*) FROM sync_issues")).scalar_one()
        fields = (
            conn.execute(
                text("SELECT field_name FROM donation_contact_changes ORDER BY field_name")
            )
            .scalars()
            .all()
        )
    assert tuple(row) == ("Ann", False, "ann@example.org", 1000)
    assert issues == 0  # a missing donor_email key in the v1 raw payload is not a "change"
    assert fields == ["donor_email", "donor_name"]
    eng.dispose()
