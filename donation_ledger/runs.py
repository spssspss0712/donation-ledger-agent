"""sync_runs bookkeeping, advisory locking, and error sanitising shared by all jobs."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from donation_ledger.models import SyncRun

log = logging.getLogger(__name__)

# Stable per-job keys for pg_try_advisory_lock (arbitrary constants).
LOCK_KEYS = {"sheet": 7_300_001, "stripe_reconcile": 7_300_002}


def safe_error_message(exc: BaseException) -> str:
    """One line, no bound SQL parameters (they may hold donor PII)."""
    if isinstance(exc, SQLAlchemyError):
        orig = getattr(exc, "orig", None)
        detail = str(orig) if orig is not None else type(exc).__name__
        first = detail.strip().splitlines()[0] if detail.strip() else ""
        return f"{type(exc).__name__}: {first}"[:300]
    return f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"[:300]


@dataclass
class Counters:
    rows_read: int = 0
    rows_inserted: int = 0
    rows_duplicate_skipped: int = 0
    rows_quarantined: int = 0
    rows_ignored: int = 0
    issues_recorded: int = 0

    def add(self, other: Counters) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


class LockNotAcquiredError(Exception):
    pass


@contextmanager
def advisory_lock(engine: Engine, source: str) -> Iterator[Connection]:
    """Hold a session-level advisory lock on a dedicated connection.

    The lock dies with the connection, so a crashed process can never leave it stuck.
    """
    key = LOCK_KEYS[source]
    conn = engine.connect()
    try:
        acquired = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar_one()
        conn.commit()
        if not acquired:
            raise LockNotAcquiredError(source)
        yield conn
    finally:
        _release(conn, key)


def _release(conn: Connection, key: int) -> None:
    # A pooled connection keeps session-level locks, so unlock explicitly. If that fails
    # the connection is broken: invalidate it so the server drops the lock with the session.
    try:
        conn.rollback()
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        conn.commit()
    except SQLAlchemyError:
        conn.invalidate()
    finally:
        conn.close()


def assert_lock_still_held(conn: Connection) -> None:
    """Fail fast if the lock connection died (then the lock is gone)."""
    conn.execute(text("SELECT 1"))
    conn.commit()


def fail_stale_runs(session: Session, source: str) -> int:
    """Only call while holding the lock: any 'running' row is then a crashed predecessor."""
    result = session.execute(
        text(
            "UPDATE sync_runs SET status='failed', finished_at=now(), "
            "error_message='run did not finish (process crashed or was killed)' "
            "WHERE source=:s AND status='running'"
        ),
        {"s": source},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def start_run(session: Session, source: str) -> int:
    run = SyncRun(source=source, status="running")
    session.add(run)
    session.flush()
    return run.id
