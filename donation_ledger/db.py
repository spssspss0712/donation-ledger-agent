"""Engine and session helpers."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    # pre_ping replaces connections the server dropped while idle.
    # Timeouts keep a stalled network or a stuck statement from hanging a webhook thread or
    # holding the sync advisory lock forever.
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        connect_args={
            "connect_timeout": 10,
            "options": "-c statement_timeout=120000 -c lock_timeout=30000",
        },
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
