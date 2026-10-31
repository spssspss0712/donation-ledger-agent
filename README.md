# donation-ledger

donation-ledger is the backend for a nonprofit's donation records. It writes donations from two sources, Stripe webhooks (real time) and a staff-maintained Google Sheet (daily sync), into one PostgreSQL ledger. Duplicates cannot occur, bad rows are quarantined instead of lost, and every job can be rerun safely after any failure.

## Architecture

```mermaid
flowchart LR
    S[Stripe] -- "webhook (signed)" --> W["POST /webhooks/stripe"]
    S -. "list recent payment intents" .-> R["reconcile job<br/>python -m donation_ledger.reconcile"]
    G[Google Sheet / CSV] -- "SheetReader" --> Y["sync job<br/>python -m donation_ledger.sync"]
    W --> L[("donations<br/>UNIQUE(source, external_id)")]
    R --> L
    Y --> L
    Y --> Q[("rejected_rows<br/>sync_issues")]
    W --> Q
    R --> Q
    Y --> H[("sync_runs")]
    R --> H
    L --> A["GET /donations, /sync-runs, /health"]
    H --> A
```

Code layout: pure logic (`parsing.py`, `stripe_mapping.py`, `sheet_rows.py`) is separate from I/O (`ledger.py`, `sync.py`, `reconcile.py`, `webhook.py`, `api.py`). External services sit behind two small interfaces: `SheetReader` (CSV and Google implementations) and `PaymentIntentSource` (Stripe implementation; tests use a fake).

## Data model

| Table | Purpose |
|-------|---------|
| `donations` | The ledger. One row per real donation. `UNIQUE(source, external_id)` is the deduplication guarantee. `source` is `stripe` or `sheet`; `external_id` is the `pi_...` ID or the sheet `Entry ID`. `amount_minor` is an integer in the smallest currency unit (cents); `currency` is uppercase ISO 4217; `donated_at` is UTC. `donor_name` is `NULL` and `is_anonymous` is true for anonymous donors (a CHECK keeps them consistent); the word "Anonymous" exists only in the API. `raw_payload` keeps the original Stripe object or sheet row. |
| `sync_runs` | One row per sync or reconcile run: source (`sheet`, `stripe_reconcile`), start/end, status (`running`, `succeeded`, `failed`, `partial`), rows read / inserted / duplicate-skipped / quarantined / ignored, warnings recorded, and a PII-free error message. |
| `rejected_rows` | Quarantine. Source, external ID (if any), the raw row, the reason, the run that last saw it, and a status: `open` (retried every run), `resolved` (a donation now exists for it), `absent` (no longer in the sheet; reopens if it returns), `ignored` (a human said stop). Identity is a content fingerprint, not a row position. |
| `sync_issues` | Warnings. Currently only `changed_after_sync`: a row already ingested was later edited in the sheet. The donation is never modified. |

## Correctness and recovery

**Why duplicates cannot occur.** Every write to `donations` is `INSERT ... ON CONFLICT (source, external_id) DO NOTHING` against a database unique constraint. There is no "check, then insert" step anywhere, so two webhook deliveries, a webhook racing the reconciliation job, or two overlapping syncs can all try at once and PostgreSQL lets exactly one row in; the others are counted as duplicates and the caller still reports success (the webhook returns 200). A Stripe donation is identified by its payment intent ID, so any number of different events for one payment still produce one row. A sheet row is identified by its `Entry ID`; rows whose ID is empty or appears more than once are quarantined, never guessed at. Amounts are parsed with `Decimal` into integers, never floats.

**How it recovers.** The sheet sync compares the *whole* sheet on every run instead of remembering a "last synced position", so a rerun after any failure is correct by construction and a quarantined row can never be skipped forever; it is simply evaluated again next time and ingested as soon as it is fixed. Each batch (default 500 rows) is one database transaction that also updates the run's counters, so a failure rolls back the whole batch and the counters always match committed data. If the process is killed (even `kill -9`), the batches already committed stay valid, nothing half-written exists, the PostgreSQL advisory lock that prevents concurrent syncs is released automatically with the connection, and the next run marks the orphaned `running` row as `failed` and finishes the job. If the database fails during a webhook, the endpoint returns 500 and Stripe retries the delivery. These behaviours are covered by tests that use a real PostgreSQL, real threads and processes, a terminated database session and a SIGKILLed process (see `tests/test_sync_faults.py`, `tests/test_webhook_api.py`).

## How to run

Requirements: Python 3.12+, PostgreSQL 16 (Docker is the easiest way).

```bash
python -m venv .venv && . .venv/bin/activate
make install
make db            # docker compose: PostgreSQL with the ledger, ledger_test and ledger_demo databases
```

(No Docker? Create three databases `ledger`, `ledger_test`, `ledger_demo` owned by a user `ledger`/`ledger` in any PostgreSQL 16, or set `DATABASE_URL`, `TEST_DATABASE_URL`, `DEMO_DATABASE_URL`.)

**Offline mode** (no credentials, no network):

```bash
make demo          # initialises a throw-away demo DB and walks through every behaviour
```

```bash
export DATABASE_URL=postgresql+psycopg://ledger:ledger@localhost:5432/ledger
alembic upgrade head
SHEET_SOURCE=csv SHEET_CSV_PATH=sample_data/sheet.csv python -m donation_ledger.sync
```

```bash
ALLOW_UNAUTHENTICATED_API=true STRIPE_WEBHOOK_SECRET=whsec_FAKE_local uvicorn donation_ledger.main:create --factory
```

**Real-integration mode** (Phase B; **not verified**, see EXPERIMENT_REPORT.md). Copy `.env.example` to `.env` and fill in: `STRIPE_WEBHOOK_SECRET` (from the Stripe dashboard/CLI), `STRIPE_API_KEY` (a **test-mode** `sk_test_...` key; live keys are refused), `SHEET_SOURCE=google`, `GOOGLE_SHEET_ID`, `GOOGLE_WORKSHEET` (optional) and `GOOGLE_SERVICE_ACCOUNT_JSON` (the JSON itself or a path; share the sheet with the service account's e-mail). Then schedule:

```bash
python -m donation_ledger.sync        # daily
python -m donation_ledger.reconcile   # e.g. hourly; looks back RECONCILE_LOOKBACK_HOURS (default 72)
```

Exit codes: `0` success, `1` failure/partial, `3` skipped because another run holds the lock. Deployment notes for Render are in `render.yaml` (written, never deployed).

The sheet must have these columns in row 1 (any order, case-insensitive): `Entry ID, Date, Donor Name, Donor Address, Amount, Currency, Recipient`. If one is renamed or missing the run fails loudly and writes nothing.

## How to test

```bash
make check         # ruff, ruff format --check, mypy --strict, pytest
make audit         # pip-audit on the pinned runtime dependencies
```

Tests need a real PostgreSQL at `TEST_DATABASE_URL` (default `.../ledger_test`; its `public` schema is **dropped and recreated** by the test session, so never point it at real data). Stripe and Google are never contacted: events are signed offline with the documented HMAC scheme and verified by the Stripe SDK's `Webhook.construct_event`.

## Design trade-offs

See `DECISIONS.md` for the full list. The main ones: full comparison every run (simple and self-healing, costs one full sheet read per day); PostgreSQL advisory lock instead of a lock table (cannot get stuck, but needs a connection that stays open for the run); PostgreSQL only (no SQLite) because the guarantees rely on its constraints; unknown/refund events are acknowledged and ignored; the read-only API hides e-mail and address and requires a bearer token.

## Known limitations and out of scope

- Refunds, disputes and any Stripe event other than `payment_intent.succeeded` are ignored (by specification).
- `Stripe` donor name/e-mail are only available when the event carries them (billing details on an expanded charge, `receipt_email`, or `metadata.donor_name`/`donor_email`). A bare PaymentIntent event has no name, so those donations are stored as anonymous.
- Reconciliation looks back a fixed window by creation time; a payment created before the window that succeeds later is missed if its webhook is also lost.
- If someone edits a synced sheet row, only a warning is stored; nobody is notified automatically. Check `sync_issues`.
- A named Stripe donor can be stored as anonymous: real webhook events carry `latest_charge` as an ID string, and the billing name lives on the charge. Only reconciliation (which expands the charge) or `metadata.donor_name` supplies it, and the append-only ledger never back-fills. If the webhook wins the race the row stays anonymous. Put the donor name in PaymentIntent metadata on the website to avoid this.
- Reconciliation cannot see payments created before its lookback window that succeed later.
- Amounts like `.5` or `10.` are quarantined (only `1000`, `1,000.50`, `$1,000.50` styles are accepted).
- Stripe `raw_payload` is stored verbatim, so a donor literally named "Anonymous" in Stripe metadata appears there (the `donor_name` column is still NULL).
- The word "Anonymous" is scrubbed from stored data only where it is a donor name (sheet `Donor Name`, any Stripe string value equal to it); in other columns (address, recipient, extra columns) it is stored as written. In Stripe, a billing name of "Anonymous" is blanked before name fallbacks run, so metadata/shipping names may then be used.
- A CSV row with an unterminated quote swallows the following lines (CSV limitation).
- Two byte-identical bad rows (same cells, no distinguishing ID) share one `rejected_rows` entry.
- The real Google Sheets reader and the real Stripe listing client were never run against the real services.
- No authentication on the webhook other than the Stripe signature; no rate limiting.
- Logs/error messages avoid donor data, but `raw_payload`, `rejected_rows.raw_row` and `sync_issues.detail` intentionally contain it: protect database access accordingly.

## Runbook: when something goes wrong

| Symptom | What to do |
|---------|------------|
| Sync exits non-zero / `sync_runs.status = failed` | Read `error_message` (`GET /sync-runs` or the table). Fix the cause (sheet columns renamed, credentials, database down) and rerun; reruns are always safe. |
| Status `partial` | Some batches committed. Just rerun; duplicates are skipped automatically. |
| Exit code 3 / "another sync is already running" | A run is in progress. If none is, a previous process may still hold its database session; the lock disappears when that session ends. |
| A `running` row that never finishes | The process died. The next run marks it `failed` automatically. |
| Rows in `rejected_rows` with status `open` | Fix the row in the sheet (it is ingested on the next run and marked `resolved`) or mark it `python -m donation_ledger.admin ignore-rejected <id>`. List them with `python -m donation_ledger.admin list-rejected`. |
| `sync_issues` rows | A synced row was edited. The ledger was *not* changed. Decide manually (e.g. correct the donation by hand in the database) and keep a note. |
| Webhook returns 400 | Wrong `STRIPE_WEBHOOK_SECRET`, body altered by a proxy, or clock skew over 5 minutes. |
| Webhook returns 5xx | Database problem. Stripe retries automatically; run the reconcile job afterwards to be sure nothing was missed. |
| Webhook returns 503 | `STRIPE_WEBHOOK_SECRET` is not set. |
| Missing Stripe donations | Run `python -m donation_ledger.reconcile` (optionally with a larger `RECONCILE_LOOKBACK_HOURS`). |
