"""Fill-once contact rules (SPEC 2.3), exercised against a real PostgreSQL."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from donation_ledger.contacts import ContactOutcome, apply_reads
from donation_ledger.ledger import NewDonation, insert_donations

WHEN = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)


def donation(ext: str = "C-1", source: str = "sheet", **kw: Any) -> NewDonation:
    base = NewDonation(
        source=source,
        external_id=ext,
        donor_name=None,
        donor_email=None,
        donor_address=None,
        recipient=None,
        amount_minor=1000,
        currency="USD",
        donated_at=WHEN,
        raw_payload={},
    )
    return replace(base, **kw)


def read(sf: sessionmaker[Session], item: NewDonation, run_id: int | None = None) -> ContactOutcome:
    """One 'read' of a donation, exactly as a webhook/reconcile/sync batch performs it."""
    with sf() as session, session.begin():
        inserted = insert_donations(session, [item])
        return apply_reads(session, [item], inserted, run_id)


def q(engine: Engine, sql: str) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql))]


def issues(engine: Engine) -> list[tuple[Any, ...]]:
    return q(engine, "SELECT external_id, kind FROM sync_issues ORDER BY id")


def test_first_read_inserts_with_contacts_and_writes_no_audit_row(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    out = read(session_factory, donation(donor_email="a@b.org", donor_name="Ann"))
    assert (out.filled, out.issues) == (0, 0)
    assert q(engine, "SELECT donor_email, donor_name, is_anonymous FROM donations") == [
        ("a@b.org", "Ann", False)
    ]
    assert q(engine, "SELECT count(*) FROM donation_contact_changes") == [(0,)]


def test_later_read_fills_an_empty_email_once_and_audits_it_without_the_value(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation())
    out = read(session_factory, donation(donor_email="late@b.org"))
    assert out.filled == 1
    assert q(engine, "SELECT donor_email FROM donations") == [("late@b.org",)]
    audit = q(
        engine,
        "SELECT d.external_id, c.field_name, c.source, c.sync_run_id, c.changed_at IS NOT NULL "
        "FROM donation_contact_changes c JOIN donations d ON d.id = c.donation_id",
    )
    assert audit == [("C-1", "donor_email", "sheet", None, True)]
    columns = {
        r[0]
        for r in q(
            engine,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='donation_contact_changes'",
        )
    }
    assert not any("value" in c for c in columns)  # the new value is only in donations


def test_audit_row_records_the_run_id_that_did_the_fill(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sync_runs (id, source) VALUES (41, 'sheet')"))
    read(session_factory, donation())
    read(session_factory, donation(donor_address="1 Road"), run_id=41)
    assert q(engine, "SELECT field_name, sync_run_id FROM donation_contact_changes") == [
        ("donor_address", 41)
    ]


def test_every_contact_field_can_be_filled_and_each_fill_is_audited(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation())
    out = read(
        session_factory,
        donation(donor_name="Ann", donor_email="a@b.org", donor_address="1 Rd", recipient="Fund"),
    )
    assert out.filled == 4
    assert q(engine, "SELECT field_name FROM donation_contact_changes ORDER BY field_name") == [
        ("donor_address",),
        ("donor_email",),
        ("donor_name",),
        ("recipient",),
    ]


def test_filling_the_name_clears_is_anonymous_in_the_same_statement(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation())
    assert q(engine, "SELECT is_anonymous FROM donations") == [(True,)]
    read(session_factory, donation(donor_name="Ann"))
    # The CHECK is_anonymous = (donor_name IS NULL) would have rejected a two-step update.
    assert q(engine, "SELECT donor_name, is_anonymous FROM donations") == [("Ann", False)]


def test_an_existing_value_is_never_overwritten_and_a_different_one_records_a_warning(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation(donor_email="first@b.org"))
    out = read(session_factory, donation(donor_email="other@b.org"))
    assert (out.filled, out.issues) == (0, 1)
    assert q(engine, "SELECT donor_email FROM donations") == [("first@b.org",)]
    assert issues(engine) == [("C-1", "contact_changed_after_sync")]
    assert q(engine, "SELECT count(*) FROM donation_contact_changes") == [(0,)]


def test_the_same_value_again_is_silent(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation(donor_email="same@b.org", donor_name="Ann"))
    out = read(session_factory, donation(donor_email="same@b.org", donor_name="Ann"))
    assert (out.filled, out.issues) == (0, 0)
    assert issues(engine) == []


def test_the_same_differing_value_seen_on_every_run_is_warned_only_once(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation(donor_email="first@b.org"))
    for _ in range(3):
        read(session_factory, donation(donor_email="other@b.org"))
    assert issues(engine) == [("C-1", "contact_changed_after_sync")]


def test_a_later_read_without_a_value_never_blanks_a_stored_value(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation(donor_email="keep@b.org", donor_name="Ann", recipient="F"))
    out = read(session_factory, donation())  # everything empty/anonymous now
    assert (out.filled, out.issues) == (0, 0)
    assert q(engine, "SELECT donor_email, donor_name, is_anonymous, recipient FROM donations") == [
        ("keep@b.org", "Ann", False, "F")
    ]


def test_warning_details_never_contain_the_contact_values(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation(donor_email="first@b.org", donor_address="12 Secret Lane"))
    read(session_factory, donation(donor_email="other@b.org", donor_address="99 Other Street"))
    blob = " ".join(r[0] for r in q(engine, "SELECT detail::text FROM sync_issues"))
    for secret in ("first@b.org", "other@b.org", "Secret Lane", "Other Street"):
        assert secret not in blob
    assert "donor_email" in blob


def test_money_fields_are_never_changed_and_a_difference_records_changed_after_sync(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation("pi_M1", "stripe"))
    out = read(
        session_factory,
        donation(
            "pi_M1",
            "stripe",
            amount_minor=999999,
            currency="EUR",
            donated_at=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    )
    assert out.issues == 1
    assert q(engine, "SELECT amount_minor, currency, donated_at FROM donations") == [
        (1000, "USD", WHEN)
    ]
    assert q(engine, "SELECT kind, detail->'changed_fields' FROM sync_issues") == [
        ("changed_after_sync", ["amount_minor", "currency", "donated_at"])
    ]


def test_malformed_email_on_first_read_records_invalid_email_once_and_stores_null(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    item = donation(invalid_email="not-an-email")
    read(session_factory, item)
    read(session_factory, item)
    assert q(engine, "SELECT donor_email FROM donations") == [(None,)]
    assert issues(engine) == [("C-1", "invalid_email")]
    assert "not-an-email" not in q(engine, "SELECT detail::text FROM sync_issues")[0][0]


def test_a_valid_email_arriving_after_a_malformed_one_fills_the_field(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation(invalid_email="oops"))
    read(session_factory, donation(donor_email="fixed@b.org"))
    assert q(engine, "SELECT donor_email FROM donations") == [("fixed@b.org",)]
    assert q(engine, "SELECT field_name FROM donation_contact_changes") == [("donor_email",)]


def test_an_application_level_money_rewrite_is_blocked_by_the_database_trigger(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation())
    with pytest.raises(Exception, match="immutable"), session_factory() as s, s.begin():
        s.execute(text("UPDATE donations SET amount_minor = 1"))


def test_concurrent_fills_with_different_values_keep_exactly_one_and_audit_once(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation())
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            barrier.wait()
            read(session_factory, donation(donor_email=f"writer{n}@b.org"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    stored = q(engine, "SELECT donor_email FROM donations")[0][0]
    assert stored in {f"writer{n}@b.org" for n in range(8)}
    assert q(engine, "SELECT count(*) FROM donation_contact_changes") == [(1,)]
    # Every loser saw a different value than the winner's and left a warning for a human.
    assert q(
        engine, "SELECT count(*) FROM sync_issues WHERE kind='contact_changed_after_sync'"
    ) == [(7,)]


def test_concurrent_fills_with_the_same_value_audit_once_and_warn_nobody(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    read(session_factory, donation())
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        read(session_factory, donation(donor_email="same@b.org"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert q(engine, "SELECT donor_email FROM donations") == [("same@b.org",)]
    assert q(engine, "SELECT count(*) FROM donation_contact_changes") == [(1,)]
    assert issues(engine) == []
