# PROGRESS

## Milestones
- M1-M7 (scaffold, parsing, Stripe webhook + API, sheet sync, fault injection, reconcile, CLI/demo/CI/docs): done.
- M9 adversarial review: 12 independent-reviewer rounds with fixes after each (see EXPERIMENT_REPORT.md section 3).
- M10 cold start: a reviewer cloned the branch and followed README.md literally (venv, make check, make audit, demo, sync, uvicorn + signed curl): all commands worked; README gaps found there were fixed.

## Current state
225 tests pass (PostgreSQL 16; Python 3.12 and 3.13); ruff, ruff format, mypy --strict, pip-audit clean; GitHub Actions green on recent commits.

## Known issues / not done
- Phase B (real Stripe/Google) and Phase C (Render) not attempted: no credentials. Google reader, real Stripe listing client and render.yaml are unverified.
- Stripe donor name often missing from webhook events (needs a product decision, see BLOCKERS.md).
