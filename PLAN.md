# PLAN

Scope: Phase A only (offline, no credentials). Branch `agent/build`.

## Milestones and acceptance checks

| # | Milestone | Acceptance check |
|---|-----------|------------------|
| M1 | Scaffold: pins, config, db, models, Alembic migration | `alembic upgrade head` on an empty PG creates 4 tables; test that models == migration (no autogenerate diff) |
| M2 | Pure parsing: amount, currency, date/timezone, names | Unit tests incl. odd formats, DST, NUL, long strings |
| M3 | Stripe webhook: signature, mapping, idempotent insert, 5xx on DB failure | Signed/unsigned/tampered tests; N threads, one record; unknown events 2xx and no write |
| M4 | Sheet sync: reader interface, CSV, classification, batches, lock, quarantine, change warnings | Sample CSV result exact; rerun is a no-op; changed-after-sync; duplicate IDs quarantined |
| M5 | Fault injection | Error mid-sync, dropped connection (pg_terminate_backend), SIGKILL mid-run; rerun converges |
| M6 | Reconciliation job (fake Stripe source), Google reader (unverified) | Fake source adds missed PIs, no duplicates with webhook |
| M7 | Read-only API + JSON logs + admin CLI | API tests, anonymous shown as "Anonymous", no PII in logs |
| M8 | CI, README, DECISIONS, demo, sample data | `make demo` runs; ruff + mypy clean |
| M9 | Adversarial review rounds until two clean | Recorded in EXPERIMENT_REPORT.md |
| M10 | Cold-start check on a fresh clone | README-only steps pass |

## Risks

- Race between webhook and reconciliation inserting the same payment (unique constraint + ON CONFLICT).
- Poison rows (NUL chars, lone surrogates, oversized fields) that make a whole batch fail forever -> validate before DB.
- Quarantined rows skipped forever by a position marker -> full comparison every run, no high-water mark.
- Crash leaves a `running` sync_run and a held lock -> advisory lock is released with the connection; stale runs are marked failed by the next lock holder.
- SQLAlchemy errors embed bound parameters (PII) -> sanitize error messages before logging or storing.
- Sheet columns renamed -> fail the run loudly instead of defaulting everything.

## Test list (abridged; names say behaviour)

parsing (amounts, currency, dates, names), migration parity, webhook (valid, invalid, missing, tampered body, replay, duplicate, concurrent, unknown type, out-of-order events, multiple events same PI, bad payload quarantined, DB failure -> 500, oversized body), sync (sample CSV, idempotent rerun, quarantine retry and resolve, ignored rows, changed-after-sync warning de-duplicated, deleted rows ignored, renamed columns fail, concurrent runs -> one wins, stale run cleanup, batch rollback), fault injection, reconcile, API, logging.
