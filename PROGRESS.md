# PROGRESS

## Milestones
- M1 scaffold, schema, Alembic migration: done (tests: migration creates tables; models == migration).
- M2 parsing helpers: done (59 parametrised unit tests).
- M3 Stripe webhook + API + logging: done, real-PG tests including 16-thread race.
- M4 sheet sync, quarantine, change warnings, lock: done.
- M5 fault injection (error mid-sync, killed DB session, killed lock session, SIGKILL, concurrent runs): done.
- M6 reconciliation (fake Stripe source), Google reader (unverified): done.
- M7 CLI/admin/demo/CI/docs: done.
- M9 adversarial review: see EXPERIMENT_REPORT.md.

## Current state
All tests, ruff, ruff format, mypy --strict and pip-audit pass locally on Python 3.12 and 3.13 against PostgreSQL 16.

## Next step
Final cold-start check from a fresh clone; confirm GitHub Actions run.

## Known issues
- Real Stripe / Google / Render paths never exercised (no credentials).
