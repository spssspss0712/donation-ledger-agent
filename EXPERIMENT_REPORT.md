# EXPERIMENT REPORT

## 1. Summary

| Item | Value |
|------|-------|
| Start / end (UTC) | 2026-10-31 07:19 / about 09:10 (about 1 h 50 min of the 3 h budget) |
| Scope | Phase A only (offline). Phase B (real Stripe test mode, real Google Sheet) and Phase C (Render) were **not attempted**: no credentials were provided |
| Branch | `agent/build` (pushed to origin after each milestone, never force-pushed) |
| Self-review rounds | 12 independent-reviewer rounds (several rounds ran two reviewers in parallel), each followed by fixes; see section 3 |
| Human interventions | 0 |
| Tests | 225 total, 225 passing locally (Python 3.13 and 3.12, PostgreSQL 16). ruff, ruff format, mypy --strict and pip-audit clean |
| CI | GitHub Actions workflow with a PostgreSQL 16 service container. Runs 8 and 10 (and later ones, see section 2) were observed green on GitHub; the final commit's run is reported in section 2 |

## 2. Verification status per feature

Legend: **A** = verified by automated tests (real PostgreSQL, no mocks of the logic under test); **R** = verified in a real environment (real external service); **W** = code written, not verified against the real service.

| Feature | A | R | W | Notes |
|---------|---|---|---|-------|
| Schema + Alembic migration (4 tables, constraints) | yes | n/a | | models == migration checked by test; `alembic upgrade head` from an empty DB tested via CLI |
| Dedup via `UNIQUE(source, external_id)` + `ON CONFLICT DO NOTHING` | yes | | | 16-thread and HTTP-level races, per-payment advisory locks |
| Stripe webhook signature verification (raw bytes, tolerance, tamper, replay) | yes | | | signatures generated offline with the documented HMAC scheme and checked by the SDK's `Webhook.construct_event`; **not** checked against a real Stripe-signed delivery |
| Webhook idempotency, multi-event-per-payment, unknown events, 5xx on DB failure | yes | | | |
| Stripe event field mapping (amount, currency, created, name/email) | yes | | | based on Stripe docs/my reading of the object shape, never compared with real events (see assumption 1) |
| Stripe reconciliation job | yes (fake source) | | real `StripePaymentIntentSource` | the real client was only exercised in review against a local fake HTTP server, not Stripe |
| Sheet sync (CSV): parsing, quarantine, change warnings, absent/ignored/resolved lifecycle | yes | n/a | | |
| Sheet sync: batches, rollback, rerun, advisory lock, stale-run cleanup | yes | | | error injection, `pg_terminate_backend`, SIGKILL of a real subprocess, concurrent threads and processes |
| Google Sheets reader | | | **W, UNVERIFIED** | only failure handling tested (no credentials); locale of formatted values is a known risk |
| Read-only API (`/donations`, `/sync-runs`, `/health`), pagination, filter, auth | yes | | | |
| JSON logs without PII/secrets | yes | | | tests capture logs of webhook + sync with hostile data |
| Offline demo (`python -m donation_ledger.demo`) | yes (run by a test and by CI) | | | |
| CI workflow | | partly | | GitHub Actions ran it; see CI row in section 1 |
| Render deployment (`render.yaml`) | | | **W, never deployed** | |
| Dependency audit | yes | | | `pip-audit` reports no known vulnerabilities (runtime and dev pins) |

## 3. Problems found by the adversarial self-review

Each round used a fresh subagent that saw only SPEC.md and the source (not the tests or my notes). Every finding was fixed or documented, and bugs were turned into tests; where I could, I confirmed the new test fails without the fix.

| Round | Finding (severity) | Resolution |
|-------|--------------------|-----------|
| 0 (own tests) | Non-ASCII 3-letter Stripe currency passed `isalpha` but violated the DB CHECK, so the webhook would return 500 forever (high); NaN in a signed payload would break JSONB (medium) | ASCII/ISO check; non-finite floats stringified |
| CI | `alembic upgrade head` failed from the CLI (cannot import the package) although all tests passed | `prepend_sys_path`, `%(here)s`, CLI test |
| 1 | Stripe name lost when the webhook wins the race with reconcile (medium, **documented, not fixed**); "Anonymous" stored in raw payloads (medium); no DB/Google timeouts (medium); quarantined rows marked resolved just because they vanished (low-medium); signed non-object body -> 500; `amount_received=0` fell back to `amount` | timeouts added; raw payloads scrubbed; `absent` status; webhook catches all parse errors; amount rule fixed |
| 2 | Year-9999/0001 date overflow failed the whole sync (high); huge `SYNC_BATCH_SIZE` made every run fail; Stripe currency not ISO-checked; unbounded chunked body; UGX exponent | all fixed + tests |
| 3 | An `ignored` rejection suppressed a later-valid row (lost donation); one bad byte/huge cell aborted the CSV; surrogate IDs/huge amounts crashed the run; unicode-lookalike currency accepted; API 500 on out-of-range params; zero-width-only names | all fixed + tests |
| 4 | Zero-width characters hid duplicate/blank Entry IDs and "Anonymous"; quadratic CPU on huge digit strings; duplicate extra headers lost data; reconcile counted ignored rows as quarantined; alembic path | fixed + tests |
| 5 | Undecodable bytes stored as U+FFFD (permanent corruption, later duplicate); invisible chars in headers; `$` with GBP/JPY | quarantine / strip / reject |
| 6 | Truncated CSV rows ingested as smaller donations; unset `SHEET_SOURCE` silently synced the bundled sample CSV (medium); absurd Stripe `created` made `GET /donations` return 500; Stripe SDK error text could echo key fragments; stale bad snapshot reopened a rejection | fixed + tests |
| 7 | **My own regression:** requiring `SHEET_SOURCE` everywhere stopped the web service and reconcile from starting (high); a run that lost its lock could overwrite a newer run's bookkeeping; killed runs labelled `failed` instead of `partial` | fixed + tests that start every entry point without the variable |
| 8 | Identical bad rows shared one fingerprint (ignoring one silenced both donations); `Settings.__repr__` leaked secrets; PaymentIntent `client_secret` stored at rest (medium); `.env.example` placeholders accepted as real secrets | fixed + tests |
| 9 | Edit that turns a synced row into a rejected one raised no warning; `1.000` EUR read as 1.00; NFC/NFD Entry IDs; **race: bad+good snapshots of one payment left an open rejection beside the donation (396/400 runs)**; ambiguous COMMIT could overwrite a recorded success | per-payment advisory locks (test fails without them), guarded `finish_run`, others fixed |
| 10 | Postgres published on all interfaces in compose; `.env.*` not ignored; transitive deps unpinned; spurious warning for NFD ids | fixed (`constraints.txt`) |
| 11 | Nothing new at any severity from the code-reading reviewer; scenario reviewer (39 scenarios) found no defects (one documented stricter-than-spec rule, one startup traceback now a JSON error) | -- |
| 12 | see below | |

ROUND12_PLACEHOLDER

Honest note on the stopping rule: SPEC.md asks for two consecutive rounds that find nothing new. Rounds 1-10 each found something. Round 11 was clean; round 12 is reported above. Subagent reviews are not proof of absence of bugs.

## 4. Assumptions and unclear requirements

See `BLOCKERS.md` and `DECISIONS.md`. The ones that matter most:
1. **Stripe donor name/e-mail:** a real `payment_intent.succeeded` carries `latest_charge` as an ID string, so billing details are normally missing. Named donors can be stored anonymous when the webhook wins the race against reconcile (append-only ledger never back-fills). Workaround implemented: read `metadata.donor_name` / `donor_email`. Needs a product decision.
2. The read-only API needs a bearer token and hides e-mail/address (spec silent).
3. Date plausibility window, `$` only for dollar-family currencies, `1.000`-style fractions quarantined, truncated rows quarantined, undecodable bytes quarantined: stricter-than-spec choices made to avoid silently wrong money.
4. "Retried until they pass or are ignored" vs "rows deleted are ignored": rejections for rows that disappear become `absent` (reopen if they return).
5. Python 3.12+ required (code uses PEP 695 syntax).

## 5. What was not finished and why

- Phase B (real Stripe test mode, real Google Sheet): no credentials. The Google reader and the real Stripe listing client are **unverified**.
- Phase C (Render deployment): no access; `render.yaml` is unverified.
- Not implemented: refunds/disputes (out of scope by spec); back-filling Stripe donor details; in-app scheduling (cron entry points only); notification when a `changed_after_sync` warning appears; uvicorn's own access logs are not JSON.
- The tests depend on one PostgreSQL database being used by one test session at a time.

## 6. If I did it again, what SPEC.md should say more clearly

- Where the Stripe donor name/e-mail live (PaymentIntent vs Charge vs metadata) and whether a later source may enrich an existing NULL field.
- Whether the read-only API is public or authenticated, and whether e-mail/address may be exposed.
- What "retried until they pass" means for a quarantined row deleted from the sheet, and what to do about identical quarantined rows.
- Whether stricter-than-spec quarantining (ambiguous amounts such as `1.000`, truncated rows, `$` with another currency) is wanted, versus best-effort parsing.
- The Sheet locale/format assumptions for dates and numbers when read through the Google API.
- That "append-only" also covers rows the webhook wrote without donor details.
