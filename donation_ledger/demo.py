"""Offline end-to-end demo. ``make demo`` (needs a local PostgreSQL, see README).

The demo DROPS ALL TABLES in the target database, so it refuses to run unless the database
name contains "demo".
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text
from sqlalchemy.engine import make_url

from donation_ledger.api import create_app
from donation_ledger.config import Settings, normalize_database_url
from donation_ledger.db import make_engine
from donation_ledger.sources import CsvSheetReader
from donation_ledger.sync import run_sync

# Clearly fake: only used to sign the demo's own fake events.
DEMO_WEBHOOK_SECRET = "whsec_FAKE_demo_secret_not_real"  # noqa: S105
SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"


def say(title: str, text_: str = "") -> None:
    print(f"\n=== {title} ===")
    if text_:
        print(text_)


def table(engine: Engine, sql: str) -> str:
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        header = list(result.keys())
        data = [[("" if v is None else str(v)) for v in row] for row in result]
    widths = [
        max(len(h), *(len(r[i]) for r in data)) if data else len(h) for i, h in enumerate(header)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in data]
    return "\n".join(lines)


def count(engine: Engine, name: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {name}")).scalar_one())


def main() -> int:
    from tests.helpers import sign  # the same signer the tests use

    url = normalize_database_url(
        os.environ.get(
            "DEMO_DATABASE_URL", "postgresql+psycopg://ledger:ledger@localhost:5432/ledger_demo"
        )
    )
    db_name = make_url(url).database or ""
    if "demo" not in db_name:
        print(f"refusing to wipe database {db_name!r}: its name must contain 'demo'")
        return 2
    engine = make_engine(url)

    say(
        "1. Initialise the database", "Dropping the demo schema and running `alembic upgrade head`."
    )
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    os.environ["DATABASE_URL"] = url
    command.upgrade(Config(str(SAMPLES.parent / "alembic.ini")), "head")
    print(
        "tables:",
        table(
            engine, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"
        ).replace("\n", " | "),
    )

    settings = Settings(
        database_url=url, stripe_webhook_secret=DEMO_WEBHOOK_SECRET, allow_unauthenticated_api=True
    )
    client = TestClient(create_app(settings, engine))

    body = (SAMPLES / "stripe_events" / "payment_intent_succeeded.json").read_bytes()
    say("2. Same correctly signed Stripe event delivered twice")
    for attempt in (1, 2):
        resp = client.post(
            "/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": sign(body, DEMO_WEBHOOK_SECRET)},
        )
        print(f"delivery #{attempt}: HTTP {resp.status_code} {resp.json()}")
    print(f"donations rows: {count(engine, 'donations')}  (expected 1)")
    anon = (SAMPLES / "stripe_events" / "charge_refunded.json").read_bytes()
    resp = client.post(
        "/webhooks/stripe",
        content=anon,
        headers={"Stripe-Signature": sign(anon, DEMO_WEBHOOK_SECRET)},
    )
    print(
        f"refund event (out of scope): HTTP {resp.status_code}, donations still {count(engine, 'donations')}"
    )

    say("3. Event with a wrong signature")
    resp = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sign(body, "whsec_FAKE_attacker_guess")},
    )
    print(
        f"HTTP {resp.status_code} {resp.json()}  -> rejected, donations still {count(engine, 'donations')}"
    )

    say("4. Sync a CSV that contains bad data")
    result = run_sync(engine, CsvSheetReader(SAMPLES / "sheet.csv"), "America/Los_Angeles")
    print(f"status={result.status} {result.counters}")
    print("\nquarantined rows:")
    print(
        table(
            engine,
            "SELECT id, coalesce(external_id,'-') AS entry_id, reason FROM rejected_rows ORDER BY id",
        )
    )

    say("5. Failure in the middle of a sync, then a rerun")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "second.csv"
        # The same sheet as step 4 plus 12 new rows (the sync always compares the whole sheet).
        with path.open("w", newline="", encoding="utf-8") as fh:
            fh.write((SAMPLES / "sheet.csv").read_text(encoding="utf-8"))
            w = csv.writer(fh)
            for i in range(12):
                w.writerow(
                    [
                        f"D-{i:02d}",
                        "2024-05-01",
                        f"Donor {i}",
                        "",
                        f"{i + 1}.00",
                        "USD",
                        "General Fund",
                    ]
                )
        before = count(engine, "donations")
        seen = {"inserts": 0}

        def crash_on_third_batch(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
            if statement.lstrip().upper().startswith("INSERT INTO DONATIONS"):
                seen["inserts"] += 1
                if seen["inserts"] == 3:
                    raise RuntimeError("simulated crash in the third batch")

        event.listen(engine, "before_cursor_execute", crash_on_third_batch)
        try:
            failed = run_sync(engine, CsvSheetReader(path), "America/Los_Angeles", batch_size=4)
        finally:
            event.remove(engine, "before_cursor_execute", crash_on_third_batch)
        print(f"run 1: status={failed.status} error={failed.error!r}")
        print(
            f"rows after the failed run: +{count(engine, 'donations') - before} (whole batches only; the failing batch rolled back)"
        )
        again = run_sync(engine, CsvSheetReader(path), "America/Los_Angeles", batch_size=4)
        print(
            f"run 2: status={again.status} inserted={again.counters.rows_inserted} "
            f"already-present={again.counters.rows_duplicate_skipped}"
        )
        print(
            f"rows after the rerun: +{count(engine, 'donations') - before} (expected +12, no duplicates)"
        )
        with engine.connect() as conn:
            dupes = conn.execute(
                text(
                    "SELECT count(*) FROM (SELECT 1 FROM donations GROUP BY source, external_id "
                    "HAVING count(*) > 1) d"
                )
            ).scalar_one()
        print(f"duplicate (source, external_id) groups: {dupes}")

    say("6. Final tables")
    print("donations:")
    print(
        table(
            engine,
            "SELECT id, source, external_id, coalesce(donor_name,'(anonymous)') AS donor, "
            "amount_minor, currency, donated_at FROM donations ORDER BY id",
        )
    )
    print("\nsync_runs:")
    print(
        table(
            engine,
            "SELECT id, source, status, rows_read, rows_inserted, rows_duplicate_skipped, "
            "rows_quarantined, left(coalesce(error_message,''),40) AS error FROM sync_runs ORDER BY id",
        )
    )
    print("\nrejected_rows:")
    print(
        table(
            engine,
            "SELECT id, source, coalesce(external_id,'-') AS ext, status FROM rejected_rows ORDER BY id",
        )
    )
    print("\nGET /donations (anonymous donors are shown as 'Anonymous'):")
    print(
        json.dumps(
            [
                (i["external_id"], i["donor_name"])
                for i in client.get("/donations?limit=5").json()["items"]
            ]
        )
    )
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
