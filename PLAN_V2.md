# PLAN_V2

Scope: bring the v1 implementation (branch `agent/build`, 226 tests green at start) up to SPEC.md
version 2. Phase A only (offline). Branch `agent/v2`. v1 `PLAN.md` / `EXPERIMENT_REPORT.md` stay as they are.

## 1. Differences between the v1 implementation and SPEC v2

| # | SPEC v2 requirement | v1 state | Work |
|---|---|---|---|
| D1 | Sheet has 8 columns incl. `Donor Email`; header must contain all eight | 7 columns | `sheet_rows.REQUIRED_COLUMNS`, sample CSVs, tests |
| D2 | Email rules: trim, lowercase, <=254, simple syntax; missing -> NULL; malformed -> NULL + raw kept + `invalid_email` warning | Sheet: no email at all. Stripe: lowercases anything with `@` | new `parsing.normalize_email`, used by both sources |
| D3 | Stripe donor details: name = `metadata.donor_name`; email = `receipt_email`, fallback `metadata.donor_email`. Charge billing details / `charges` / `shipping` out of scope | Reads billing_details, `charges.data[0]`, `shipping.name`, `metadata.name/email` | rewrite `stripe_mapping._donor`; drop `expand=data.latest_charge` in the real Stripe source; change the 3 tests that used billing details |
| D4 | Money immutable; contact fields fill-once with conditional UPDATE, `is_anonymous` updated in same statement | Fully append-only (insert `ON CONFLICT DO NOTHING`, warn only) | new `ledger.apply_existing_donation` (fill + warnings), used by webhook, reconcile, sync |
| D5 | Audit table `donation_contact_changes` (no values) | missing | model + migration 0002 |
| D6 | DB trigger rejecting UPDATEs of money fields + direct-SQL test | missing | migration 0002 trigger + test |
| D7 | Warnings `contact_changed_after_sync`, `invalid_email`, money `changed_after_sync` (on every path) | sheet-only `changed_after_sync` over raw text incl. contact fields | split: money vs contact; Stripe duplicates also warn on money change |
| D8 | Never refuse/quarantine because of contact info | Sheet quarantines over-long address/recipient/name | truncate + warn instead (see DECISIONS); NUL/undecodable bytes stay quarantined (Strictness) except in the email cell |
| D9 | `GET /followups` (token, paginated, reason, no email/address) | missing | new endpoint |
| D10 | Whole-batch atomicity covers fills, audit rows, warnings, quarantine records, counters; test must prove it | batch is one txn already; no test of contact rows / quarantine rolled back | tests with fault injection after each kind of write |
| D11 | Existing data survives; new migration; test upgrading a populated v1 DB | one migration | `0002`, test using a scratch DB populated at revision 0001 |
| D12 | Sample data: sheet with bad emails, second CSV completing contacts, Stripe events (receipt_email / metadata / neither) | 7-col sheet, billing-details event | new files |
| D13 | Demo steps 6-7 (second CSV, fills, warning, audit, follow-up list) | 6 steps | extend `demo.py` |
| D14 | Docs: README policy section, DECISIONS, EXPERIMENT_REPORT_V2, BLOCKERS | v1 docs | update |

## 2. Milestones and acceptance checks

| M | Content | Acceptance |
|---|---|---|
| M1 | Migration 0002 (table, `sync_runs.contacts_filled`, trigger), models, conftest truncate list | alembic-vs-models parity test green; trigger test (direct SQL UPDATE of each money column fails, contact UPDATE passes); v1->v2 upgrade test on a populated DB |
| M2 | `normalize_email` + unit tests | table-driven tests incl. 254/255 chars, whitespace, two `@`, no-dot domain, case |
| M3 | Stripe mapping per D3 | unit tests: receipt_email, metadata fallback, metadata name, billing details ignored, invalid email -> NULL + flag |
| M4 | `ledger` contact machinery | real-PG tests: fill NULL, never overwrite, never NULL-out, name fill flips `is_anonymous`, audit row without value, differing value -> warning, concurrent fills (threads) -> one winner, one audit row |
| M5 | Webhook + reconcile wiring | duplicate delivery fills; reconcile fills; money change warns; 5xx and nothing persisted when audit insert fails |
| M6 | Sheet sync 8 columns + wiring | existing sync tests adapted (recorded), sample CSV expected result, second-CSV fill/warn/no-money-change, header missing email column fails with nothing written, batch-atomicity tests |
| M7 | `/followups` | auth, pagination, reasons, no PII in body |
| M8 | Samples, demo, README, DECISIONS, PROGRESS | `make demo` shows all 7 steps; ruff + mypy clean |
| M9 | Adversarial review (subagent + own break tests + break-the-code table), repeat until two clean rounds | `EXPERIMENT_REPORT_V2.md` |
| M10 | Cold start on a fresh clone | README-only: install, tests, demo |

## 3. Risks

- **Bogus warnings after upgrade**: v1 rows have no `donor_email` key in `raw_payload.fields`; a naive raw-text diff would warn for every row. Contacts are therefore compared against the `donations` columns, never against `raw_payload`.
- **Contact vs. Strictness conflict** (NUL / undecodable bytes in a contact cell vs. "never quarantine for contact"): decided in DECISIONS, flagged as an assumption.
- **Fill race**: two writers with different values; conditional UPDATE `WHERE field IS NULL` + re-read of the winner for the warning.
- **Partial batch**: fills/audit/warnings must live in the same transaction as inserts and counters.
- **Trigger invisible to Alembic autogenerate**: covered by its own test, not the parity test.
- **PII in `sync_issues`**: details hold field names and a short digest, never emails/addresses.
- **Same email on a case/space variant** must not warn (normalised before compare).

## 4. New or changed tests (all names state behaviour)

New: migration (parity incl. new table, trigger rejects each money column, trigger allows contact fill, v1->v2 upgrade keeps rows and NULL email, v2 sync fills a v1 row without bogus warnings), email parsing table, Stripe donor mapping, fill-once (7+ cases), audit, concurrency, atomicity (sync batch with fills+quarantine+warnings rolled back; webhook), reconcile fill, followups API, 8-column header, second-CSV scenario, demo.
Changed (each recorded in DECISIONS.md): `HEADER`/`good()` helpers and literal rows (8 columns); sample-CSV expectations; the three billing-details Stripe tests; the sheet "changed after sync" tests for contact fields (now fill-once / `contact_changed_after_sync`); too-long address/recipient/name tests (now truncated, not quarantined).
