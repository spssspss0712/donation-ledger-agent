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
    Y --> Q[("rejected_rows<br/>sync_issues<br/>donation_contact_changes")]
    W --> Q
    R --> Q
    Y --> H[("sync_runs")]
    R --> H
    L --> A["GET /donations, /followups, /sync-runs, /health"]
    H --> A
```

Code layout: pure logic (`parsing.py`, `stripe_mapping.py`, `sheet_rows.py`) is separate from I/O (`ledger.py`, `contacts.py`, `sync.py`, `reconcile.py`, `webhook.py`, `api.py`). External services sit behind two small interfaces: `SheetReader` (CSV and Google implementations) and `PaymentIntentSource` (Stripe implementation; tests use a fake).

## Data model

| Table | Purpose |
|-------|---------|
| `donations` | The ledger. One row per real donation. `UNIQUE(source, external_id)` is the deduplication guarantee. `source` is `stripe` or `sheet`; `external_id` is the `pi_...` ID or the sheet `Entry ID`. `amount_minor` is an integer in the smallest currency unit (cents); `currency` is an uppercase three-letter code (any code Stripe reports is stored; sheet currencies must be valid ISO 4217 codes, otherwise the row is quarantined); `donated_at` is UTC. `donor_name` is `NULL` and `is_anonymous` is true for anonymous donors (a CHECK keeps them consistent); the word "Anonymous" exists only in the API. `donor_email` is trimmed, lowercased and syntax-checked; `NULL` if missing or malformed. `raw_payload` keeps the original Stripe object or sheet row (including the original email text). **Money fields** (`amount_minor`, `currency`, `donated_at`, `source`, `external_id`) cannot be changed by any `UPDATE` (database trigger). **Contact fields** (`donor_name`, `donor_email`, `donor_address`, `recipient`) can be filled in once while `NULL`. |
| `sync_runs` | One row per sync or reconcile run: source (`sheet`, `stripe_reconcile`), start/end, status (`running`, `succeeded`, `failed`, `partial`), rows read / inserted / duplicate-skipped / quarantined / ignored, warnings recorded, contact fields filled, and a PII-free error message. |
| `rejected_rows` | Quarantine. Source, external ID (if any), the raw row, the reason, the run that last saw it, and a status: `open` (retried every run), `resolved` (a donation now exists for it), `absent` (no longer in the sheet; reopens if it returns), `ignored` (a human said stop). Identity is a content fingerprint, not a row position. |
| `sync_issues` | Warnings for a human, de-duplicated. Kinds: `changed_after_sync` (a money field differs on a later read; nothing is changed), `contact_changed_after_sync` (a stored contact value differs from a later read; nothing is changed), `invalid_email` (malformed email stored as `NULL`; the original stays in `raw_payload`), `contact_truncated` (over-long name/address/recipient cut to the limit). Details never contain contact values. |
| `donation_contact_changes` | Audit of every fill-once update: donation ID, field name, source, the sync/reconcile run ID (if any), timestamp. The new value is deliberately not stored here (it is in `donations`). |

## Correctness and recovery

**Why duplicates cannot occur.** Every write to `donations` is `INSERT ... ON CONFLICT (source, external_id) DO NOTHING` against a database unique constraint. There is no "check, then insert" step anywhere, so two webhook deliveries, a webhook racing the reconciliation job, or two overlapping syncs can all try at once and PostgreSQL lets exactly one row in; the others are counted as duplicates and the caller still reports success (the webhook returns 200). A Stripe donation is identified by its payment intent ID, so any number of different events for one payment still produce one row. A sheet row is identified by its `Entry ID`; rows whose ID is empty or appears more than once are quarantined, never guessed at. Amounts are parsed with `Decimal` into integers, never floats.

**Contact-data policy.** Money fields are immutable: a database trigger rejects any `UPDATE` that changes `amount_minor`, `currency`, `donated_at`, `source` or `external_id`, so even a bug in the application cannot rewrite money; if a later read of the same donation disagrees, nothing changes and a `changed_after_sync` warning is recorded. Contact fields are filled in once: when a stored contact field is `NULL` and a later read (a duplicate webhook delivery, the reconciliation job, or the next sheet sync) provides a valid value, exactly that field is set with `UPDATE ... WHERE field IS NULL` (so concurrent writers cannot overwrite each other) and one audit row is written, in the same transaction. A stored value is never overwritten and never blanked; a different later value only produces a `contact_changed_after_sync` warning for a human. **A missing or malformed email never blocks a donation**, because the money has already been received: the donation is stored with `donor_email = NULL`, a malformed value is kept in `raw_payload` with an `invalid_email` warning, and `GET /followups` lists donations that still need an email (without ever returning contact data).

**How it recovers.** The sheet sync compares the *whole* sheet on every run instead of remembering a "last synced position", so a rerun after any failure is correct by construction and a quarantined row can never be skipped forever; it is simply evaluated again next time and ingested as soon as it is fixed. Each batch (default 500 rows) is one database transaction that holds everything the batch writes (new donations, contact fills and their audit rows, quarantine records, warnings, and the run's counters), so a failure rolls back the whole batch and nothing from it remains; the counters always match committed data. If the process is killed (even `kill -9`), the batches already committed stay valid, nothing half-written exists, the PostgreSQL advisory lock that prevents concurrent syncs is released automatically with the connection, and the next run marks the orphaned `running` row as `failed` and finishes the job. If the database fails during a webhook, the endpoint returns 500 and Stripe retries the delivery. These behaviours are covered by tests that use a real PostgreSQL, real threads and processes, a terminated database session and a SIGKILLed process (see `tests/test_sync_faults.py`, `tests/test_sync_contacts.py`, `tests/test_adversarial_v2.py`, `tests/test_webhook_api.py`).

## How to run

Requirements: Python 3.12+, PostgreSQL 16 (Docker is the easiest way).

```bash
python -m venv .venv && . .venv/bin/activate
make install      # pinned runtime deps + constraints.txt for transitive versions
make db            # docker compose: PostgreSQL with the ledger, ledger_test and ledger_demo databases
```

(No Docker? In any PostgreSQL 16, connected as a superuser to the `postgres` database: `CREATE USER ledger WITH PASSWORD 'ledger' CREATEDB; CREATE DATABASE ledger OWNER ledger; CREATE DATABASE ledger_test OWNER ledger; CREATE DATABASE ledger_demo OWNER ledger;` — or point `DATABASE_URL`, `TEST_DATABASE_URL`, `DEMO_DATABASE_URL` at your own databases. The Makefile defaults to the `ledger*` databases above. `make demo` **wipes** the `*demo*` database it uses and refuses any other name.)

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

Without `API_TOKEN` (or the local-only `ALLOW_UNAUTHENTICATED_API=true`) `GET /donations`, `GET /followups` and `GET /sync-runs` answer 503; with a token set, send `Authorization: Bearer <token>` (wrong/missing = 401). `GET /health` is always open. `GET /donations` and `GET /followups` take `limit` (1-200), `source` (`stripe`/`sheet`) and `before_id` (the previous page's `next_before_id`). `GET /followups` returns `donation_id, source, external_id, date, reason` (`missing_email` or `invalid_email`) for every donation whose email is `NULL`, never an email or address.

Send a signed sample webhook by hand (the signature is `t=<unix time>,v1=HMAC_SHA256(secret, "<t>.<raw body>")`):

```bash
f=sample_data/stripe_events/payment_intent_succeeded.json; t=$(date +%s)
s=$(printf '%s.' $t | cat - $f | openssl dgst -sha256 -hmac whsec_FAKE_local | awk '{print $NF}')
curl -X POST localhost:8000/webhooks/stripe -H "Stripe-Signature: t=$t,v1=$s" --data-binary @$f
```

Other samples: `payment_intent_succeeded_metadata.json` (donor name and email in `metadata`), `payment_intent_succeeded_anonymous.json` (no donor details at all) and `charge_refunded.json` (ignored event type). The first one carries only a `receipt_email`. `sample_data/sheet.csv` is a sheet with deliberately bad data (blank names, missing and malformed emails, odd amounts, duplicate and missing Entry IDs, a negative amount); `sample_data/sheet_update.csv` is the same sheet with some empty contact cells completed, one existing email changed and one amount changed, to demonstrate the fill-once rule.

**Real-integration mode** (Phase B; **not verified**, see EXPERIMENT_REPORT.md). Copy `.env.example` to `.env` and fill in: `STRIPE_WEBHOOK_SECRET` (from the Stripe dashboard/CLI), `STRIPE_API_KEY` (a **test-mode** `sk_test_...` key; live keys are refused), `SHEET_SOURCE=google`, `GOOGLE_SHEET_ID`, `GOOGLE_WORKSHEET` (optional) and `GOOGLE_SERVICE_ACCOUNT_JSON` (the JSON itself or a path; share the sheet with the service account's e-mail). Then schedule:

```bash
python -m donation_ledger.sync        # daily
python -m donation_ledger.reconcile   # e.g. hourly; looks back RECONCILE_LOOKBACK_HOURS (default 72)
```

Exit codes: `0` success, `1` failure/partial, `3` skipped because another run holds the lock. Deployment notes for Render are in `render.yaml` (written, never deployed).

The sheet must have these columns in row 1 (any order, case-insensitive): `Entry ID, Date, Donor Name, Donor Email, Donor Address, Amount, Currency, Recipient` (all eight; a missing or duplicated one fails the run and writes nothing). The sheet is read as display strings and an English (US) spreadsheet locale is assumed: dates are ISO 8601 or `M/D/YYYY`, numbers use `.` as the decimal separator and `,` as the thousands separator.

## How to test

```bash
make check         # ruff, ruff format --check, mypy --strict, pytest
make audit         # pip-audit on the pinned runtime dependencies
python scripts/break_it.py   # optional, slow: weakens each safety property in turn and checks that a test fails
```

Run one test session at a time per database (some tests assert that no advisory locks are held). The migration upgrade test creates and drops a scratch database next to the test database, so the database role needs `CREATEDB` (the docker-compose / CI `ledger` role has it). Tests need a real PostgreSQL at `TEST_DATABASE_URL` (default `.../ledger_test`; its `public` schema is **dropped and recreated** by the test session, so never point it at real data). Stripe and Google are never contacted: events are signed offline with the documented HMAC scheme and verified by the Stripe SDK's `Webhook.construct_event`.

## Design trade-offs

See `DECISIONS.md` for the full list. The main ones: full comparison every run (simple and self-healing, costs one full sheet read per day); PostgreSQL advisory lock instead of a lock table (cannot get stuck, but needs a connection that stays open for the run); PostgreSQL only (no SQLite) because the guarantees rely on its constraints; unknown/refund events are acknowledged and ignored; the read-only API hides e-mail and address and requires a bearer token; money is immutable in the database but contact fields are fill-once (so an empty email can be completed later) instead of fully append-only.

## Known limitations and out of scope

- Refunds, disputes and any Stripe event other than `payment_intent.succeeded` are ignored (by specification).
- Stripe donor details are read only from `metadata.donor_name`, `receipt_email` and `metadata.donor_email` (the website must put them there). The Charge's billing details, address and the `charge.succeeded` event are out of scope in this version (a possible future extension); a payment without them is stored with no name (anonymous) and no email, and appears in `GET /followups`. Stripe sends the donor its own receipt when `receipt_email` is set in live mode.
- Reconciliation looks back a fixed window by payment creation time; a payment created before the window that succeeds later is missed if its webhook is also lost.
- If someone edits a synced sheet row, only a warning is stored (nothing is changed); nobody is notified automatically. Check `sync_issues`.
- A named Stripe donor whose first event carries no `metadata.donor_name` is stored anonymous; a later duplicate delivery or reconciliation that carries the name fills it in once. If the stored name differs from a later one, only a warning is recorded.
- A corrected contact value cannot overwrite a wrong one automatically (by design). Read the `contact_changed_after_sync` warning and fix the row by hand.
- A sheet row with the same `Entry ID` but a different amount, currency or date is treated as the same donation: a `changed_after_sync` warning is recorded, money is untouched, and its empty contact cells are still filled. The Entry ID must therefore really be unique.
- NUL characters or undecodable bytes in a sheet cell other than `Donor Email` still quarantine the row (an email cell with them is just an invalid email). Over-long name/address/recipient text is cut (500/2000/500 characters) with a `contact_truncated` warning.
- The money trigger does not stop `DELETE`, and a table owner can drop it.
- Amounts like `.5` or `10.` are quarantined (only `1000`, `1,000.50`, `$1,000.50` styles are accepted).
- Stripe `raw_payload` is stored verbatim, so a donor literally named "Anonymous" in Stripe metadata appears there (the `donor_name` column is still NULL).
- The word "Anonymous" is scrubbed from stored data only where it is a donor name (sheet `Donor Name`, any Stripe string value equal to it); in other columns (address, recipient, extra columns) it is stored as written. In Stripe, a billing name of "Anonymous" is blanked before name fallbacks run, so metadata/shipping names may then be used.
- An amount written with `$` whose Currency column says something else (e.g. `$5` + `EUR`) is quarantined rather than guessed.
- A CSV row with an unterminated quote swallows the following lines (CSV limitation).
- The Google reader returns *formatted* cell text, so the spreadsheet's locale matters: dates must read `M/D/YYYY` or ISO and amounts must use `.` for decimals (a UK/EU-locale sheet would be misread). Unverified against a real sheet.
- A single CSV cell above 10 MB fails the whole CSV run.
- A run skipped because another holds the lock is recorded with status `failed` (error text says why); alert on the latest *non-skipped* run.
- A Stripe payment is dated by the PaymentIntent `created` time, not the time it succeeded.
- Rejected Stripe payments stay `open` until ignored (no automatic "absent" state), and a webhook and reconcile may each record one entry for the same bad payment.
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
| `sync_issues` rows | `changed_after_sync` / `contact_changed_after_sync`: a synced row or contact was edited upstream. The ledger was *not* changed. Decide manually (e.g. correct the donation by hand in the database) and keep a note. A deliberate manual correction of a money field needs the trigger switched off first (`ALTER TABLE donations DISABLE TRIGGER trg_donations_money_immutable`, then enable it again); contact fields can be corrected with a plain `UPDATE`. |
| Donors with no email | `GET /followups` lists them (`missing_email` or `invalid_email`). Fix the cell in the sheet (or Stripe metadata) and the next sync/reconcile/duplicate delivery fills it in once and records it in `donation_contact_changes`. |
| Webhook returns 400 | Wrong `STRIPE_WEBHOOK_SECRET`, body altered by a proxy, or clock skew over 5 minutes. |
| Webhook returns 5xx | Database problem. Stripe retries automatically; run the reconcile job afterwards to be sure nothing was missed. |
| Webhook returns 503 | `STRIPE_WEBHOOK_SECRET` is not set. |
| `/donations` returns 503 / 401 | 503: neither `API_TOKEN` nor `ALLOW_UNAUTHENTICATED_API=true` is configured. 401: wrong or missing bearer token. |
| Missing Stripe donations | Run `python -m donation_ledger.reconcile` (optionally with a larger `RECONCILE_LOOKBACK_HOURS`). |
