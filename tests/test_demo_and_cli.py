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
