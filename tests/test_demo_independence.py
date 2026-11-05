"""The offline demo must work from an installed package, without the tests/ directory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import stripe

from donation_ledger.stripe_signing import sign_payload
from tests.conftest import database_url_for_tests
from tests.helpers import sign as independent_sign


def test_package_signer_matches_the_independent_test_signer_and_the_stripe_sdk() -> None:
    body = json.dumps({"id": "evt_FAKE", "object": "event"}).encode()
    secret = "whsec_FAKE_demo_secret_not_real"  # noqa: S105 - fake
    assert sign_payload(body, secret, 1_700_000_000) == independent_sign(
        body, secret, 1_700_000_000
    )
    header = sign_payload(body, secret)
    stripe.Webhook.construct_event(body, header, secret)  # raises on a bad signature
    tampered = body + b" "
    try:
        stripe.Webhook.construct_event(tampered, header, secret)
    except stripe.SignatureVerificationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("tampered body must not verify")


def test_demo_source_does_not_import_the_tests_package() -> None:
    root = Path(__file__).resolve().parent.parent
    for path in (root / "donation_ledger").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from tests" not in text and "import tests" not in text, path


def test_demo_runs_with_tests_directory_not_importable(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    url = database_url_for_tests().rsplit("/", 1)[0] + "/ledger_demo"
    # Run from a directory that contains only a copy of what `make demo` needs.
    work = tmp_path / "checkout"
    work.mkdir()
    for name in ("alembic.ini", "sample_data", "migrations", "donation_ledger"):
        src = root / name
        if src.is_dir():
            subprocess.run(["cp", "-r", str(src), str(work / name)], check=True)  # noqa: S603, S607
        else:
            (work / name).write_bytes(src.read_bytes())
    env = {**os.environ, "DEMO_DATABASE_URL": url, "PYTHONPATH": str(work)}
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "donation_ledger.demo"], cwd=work, env=env, capture_output=True
    )
    assert not (work / "tests").exists()
    assert proc.returncode == 0, proc.stderr.decode()[-800:]
    assert b"money fields unchanged: True" in proc.stdout
