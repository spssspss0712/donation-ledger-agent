# PROGRESS (version 2, branch agent/v2)

Version 1 history is in git (`agent/build`) and `EXPERIMENT_REPORT.md`. This file tracks the work to bring the implementation up to SPEC.md version 2; the plan is in `PLAN_V2.md`.

## Milestones
| M | Status | Notes |
|---|--------|-------|
| M1 migration 0002 (audit table, money trigger, `contacts_filled`) | done | upgrade test on a populated v1 database, trigger tests, downgrade test |
| M2 email normalisation | done | table-driven tests |
| M3 Stripe donor details (metadata / receipt_email only) | done | billing-details tests changed (DECISIONS V2-12) |
| M4 fill-once machinery (`contacts.py`) | done | real-PG tests incl. threaded races |
| M5 webhook + reconcile wiring | done | 5xx + rollback tests |
| M6 sheet sync, eight columns | done | existing tests adapted and recorded |
| M7 `GET /followups` | done | |
| M8 samples, demo (7 steps), README, DECISIONS, BLOCKERS | done | |
| M9 adversarial review | see EXPERIMENT_REPORT_V2.md | |
| M10 cold start | see EXPERIMENT_REPORT_V2.md | |

## Known issues / not done
- Phase B (real Stripe / Google) and Phase C (Render) not attempted: no credentials. Google reader, real Stripe listing client and `render.yaml` remain unverified.
- See BLOCKERS.md for the V2 assumptions that need a human decision.
