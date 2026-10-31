"""Alembic environment. The URL comes from DATABASE_URL (or an injected connection)."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from donation_ledger.config import normalize_database_url
from donation_ledger.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is required to run migrations")
    engine = create_engine(normalize_database_url(url))
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
