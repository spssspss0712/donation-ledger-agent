import os
import subprocess
import sys

from tests.conftest import database_url_for_tests


def test_demo_refuses_to_wipe_a_database_whose_name_does_not_contain_demo() -> None:
    env = {**os.environ, "DEMO_DATABASE_URL": database_url_for_tests()}
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "donation_ledger.demo"], env=env, capture_output=True
    )
    assert proc.returncode == 2
    assert b"refusing" in proc.stdout


def test_demo_runs_end_to_end_against_a_demo_database() -> None:
    url = database_url_for_tests().rsplit("/", 1)[0] + "/ledger_demo"
    env = {**os.environ, "DEMO_DATABASE_URL": url}
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "donation_ledger.demo"], env=env, capture_output=True
    )
    out = proc.stdout.decode()
    assert proc.returncode == 0, proc.stderr.decode()[-800:]
    assert "donations rows: 1  (expected 1)" in out
    assert "HTTP 400" in out
    assert "duplicate (source, external_id) groups: 0" in out


def test_alembic_cli_upgrades_an_empty_database_from_the_repo_root() -> None:
    from sqlalchemy import create_engine, inspect, text

    url = database_url_for_tests().rsplit("/", 1)[0] + "/ledger_demo"
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    env = {**os.environ, "DATABASE_URL": url}
    proc = subprocess.run(["alembic", "upgrade", "head"], env=env, capture_output=True)  # noqa: S603, S607
    assert proc.returncode == 0, proc.stderr.decode()[-500:]
    assert "donations" in inspect(eng).get_table_names()
    eng.dispose()


def test_sync_cli_with_unreachable_database_exits_1_with_a_json_error_not_a_traceback() -> None:
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+psycopg://x:SECRETPW@127.0.0.1:1/none",
        "SHEET_SOURCE": "csv",
        "SHEET_CSV_PATH": "sample_data/sheet.csv",
    }
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "donation_ledger.sync"], env=env, capture_output=True, timeout=60
    )
    assert proc.returncode == 1
    assert b"Traceback" not in proc.stderr and b"SECRETPW" not in proc.stderr
    assert b'"level": "ERROR"' in proc.stderr
