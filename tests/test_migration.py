from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect

from donation_ledger.models import Base


def test_migration_creates_the_four_required_tables(engine: Engine) -> None:
    names = set(inspect(engine).get_table_names())
    assert {"donations", "sync_runs", "rejected_rows", "sync_issues"} <= names


def test_models_and_alembic_migration_describe_the_same_schema(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
