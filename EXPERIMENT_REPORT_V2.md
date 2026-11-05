# EXPERIMENT_REPORT_V2 (SPEC.md version 2, branch agent/v2)

The version 1 report (`EXPERIMENT_REPORT.md`) is unchanged and still describes the v1 build.

## 1. Summary

| Item | Value |
|---|---|
| First / last commit | 2026-11-05 01:44 UTC (PLAN_V2.md) / 02:30 UTC (review round 3), plus the final report commit; wall-clock stayed within the 3-hour budget. A container restart killed review round 4 once; it was re-run. |
| Tests | 380, all passing on PostgreSQL 16 (Python 3.13 locally; CI pins 3.12). v1 started at 226. ruff, ruff format, mypy --strict clean. (353 at the end of the first session; 27 added in the follow-up session.) |
| Adversarial rounds | 6 independent-reviewer rounds (a fresh subagent seeing only SPEC.md + code each time) plus my own break-it runs. Round 1: 3 real findings fixed. Round 2: 2 fixed. Round 3: 1 cosmetic accepted + 1 missing test. **Round 4: nothing new.** Round 5 (after the currency change): 1 real gap fixed (old Stripe quarantines were never re-driven) + 2 weak tests strengthened. Round 6: 1 low-severity bug fixed (stale quarantine beside an existing donation) + 1 test gap closed. |
| "Two consecutive rounds with nothing new" | **Not met.** Only round 4 was clean; rounds 5 and 6 each found something real (fixed and tested). At least two further clean rounds are still needed. |
| Human interventions | 0 (no question was asked; assumptions are in BLOCKERS.md) |
| CI | The workflow file is unchanged from v1 (PostgreSQL service, ruff, mypy, pytest, migration, demo, pip-audit). It was **not run on GitHub in this session** (no GitHub Actions access); the same commands were run locally. |
| Cold start | A fresh clone of agent/v2 (commit 5e74c04, **before** the follow-up session; not repeated afterwards), `python -m venv`, `make install`, `make check` (353 passed), `make demo` (all 7 steps) worked using a local PostgreSQL with the three databases from the README. |

## 2. Verification status of every feature

"Auto" = verified by automated tests (real PostgreSQL, real threads/processes, Stripe/Google faked only at their boundaries). "Real" = verified against a real external service: **nothing in this report was verified against real Stripe, Google or Render.**

| Feature | Status |
|---|---|
| Eight-column sheet, header failure writes nothing | Auto |
| Donor email normalisation (trim, lowercase, 254, syntax) | Auto (table tests) |
| Missing email -> NULL; malformed -> NULL + `invalid_email` + raw text kept; never quarantines | Auto (sheet, webhook, reconcile) |
| Stripe donor details from `metadata.donor_name`, `receipt_email`, `metadata.donor_email`; billing details ignored | Auto against fake events. The real PaymentIntent shape is taken from the spec text, **not** from a live event |
| Money immutable (DB trigger, all five columns) | Auto (direct SQL, second session, v1-populated database) |
| Contact fill-once via conditional UPDATE; never overwrite or blank; `is_anonymous` in the same statement | Auto incl. 8/10-thread races and webhook-vs-reconcile race |
| Audit table, no values, run id | Auto |
| Warnings `changed_after_sync`, `contact_changed_after_sync`, `invalid_email`, `contact_truncated`, de-duplicated, no contact values | Auto |
| `GET /followups` (auth, pagination, reasons, no PII) | Auto |
| Whole-batch atomicity (fills, audit, warnings, quarantine records, counters) incl. dropped connection and SIGKILL | Auto |
| Alembic 0002 upgrade of a populated v1 DB, downgrade | Auto |
| Webhook / reconcile / sync as in v1 | Auto (v1 tests still pass) |
| Offline demo, 7 steps | Auto (test) + run by hand + run in the cold-start clone |
| GitHub Actions CI | Written (unchanged), **not run here** |
| Google Sheets reader, real Stripe listing client, `render.yaml` | Code written, **not verified** (no credentials) |
| Phase B / Phase C | Not attempted |

## 3. Problems found by the adversarial self-review and how they were fixed

| Source | Problem | Fix / outcome |
|---|---|---|
| Own tests | Test of the SIGKILL-mid-fill used a different timezone than the first sync, so the fill was (correctly, at that time) skipped | Test fixed to use the same timezone; led to review of the "money differs" rule |
| Review 1 | **Real Stripe payments in valid currencies absent from my ISO list (XCG, STD, ZWG, ...) were quarantined**, so the ledger would miss received money | Codes added; still a whitelist (residual risk documented) |
| Review 1 | Reformatting a cell (`1000` -> `$1,000.00`, `2024-03-01` -> `3/1/2024`) raised a false `changed_after_sync` | Money compared by parsed value |
| Review 1 | I withheld contact fills when a row's money also changed; the spec treats the same ID as the same donation, so staff-corrected rows stayed on the follow-up list forever | Rule reversed: warn about money, still fill contacts |
| Review 2 | Lone surrogate in a sheet email cell made every INSERT fail, blocking the whole sync | Surrogates make an email invalid |
| Review 2 | Stripe emails were sanitised (NUL/surrogate stripped) before validation, silently turning a malformed address into a different valid one | Validate the original value |
| Review 3 | Removing one of two duplicate-ID rows leaves both quarantine entries `resolved` rather than one `absent` | Accepted, cosmetic (DECISIONS V2-17) |
| Review 3 | No test for a non-existent spring-forward time | Test added |
| Review 4 | Nothing new | - |

### Break-it table (scripts/break_it.py; each mutation applied alone, whole suite run, file restored)
**Real numbers.** `scripts/break_it.py` now contains **43** mutations. The first session's report said 32, which was the count at that time; the script grew to 43 (new currency, email, re-drive, lock and timezone mutations). One of the original 32, `stripe_ignores_metadata_email_fallback`, became stale after review round 2 changed the code it targeted and was not applied in later runs because I had not re-run the script; this is fixed (see below). Final complete run, one invocation, unmutated baseline green first: **43/43 mutations caught, 0 survivors, tree restored (clean `git status`)**. Examples from the first session's table:

| Property broken | Caught by |
|---|---|
| Money trigger removed (10) / ignores currency (2) / ignores donated_at (1) | `test_migration` per-column tests, `test_contacts`, `test_adversarial_v2` |
| Fill-once `IS NULL` guard removed | the two concurrent-fill tests in `test_contacts.py` (sequential tests are protected by a second, application-level guard; removing both is caught by 18 tests) |
| Blank read not ignored / name fill without `is_anonymous` / fill without audit row | `test_contacts` (30 / 10 / 19 failing) |
| Audit row written outside the batch transaction | `test_a_failing_batch_leaves_no_donations_fills_audit_rows...` |
| Quarantine record written outside the batch transaction | same atomicity tests (2) |
| Batch committed mid-way | 40 tests |
| Differing contact value / money difference / invalid email not reported | `test_contacts`, `test_sync_contacts`, webhook tests |
| Invalid email rejects the donation | 10 tests |
| Email without dot accepted, not lowercased, length limit removed | `test_parsing`, sync tests |
| Header does not require Donor Email | `test_sync_contacts` header tests (40 failing) |
| Stripe reads billing details / ignores metadata fallback | `test_stripe_mapping` |
| `/followups` leaks an email field / lists donors with email / no auth | `test_followups` |
| Webhook duplicate / reconcile do not fill | webhook and reconcile tests |
| v1: unique constraint removed, signature not verified, float amounts, timezone ignored, sync lock not taken | 40 / 6 / 4 / 6 / 2 failing tests |

The harness fix (follow-up 3): before running anything it checks that every mutation's target text (and each extra edit) matches the code exactly once; otherwise it prints the stale names and **exits 2 without running**; `tests/test_break_it_script.py` makes the same check part of every test run. It also requires a green unmutated baseline, counts a pytest crash as caught, and excludes its own test from mutation runs (that test legitimately fails under any mutation; the first attempt at the full run was aborted for this reason and restarted). Two mutations were first reported "not caught" (an equivalent mutant protected by a second guard; a mutation that pydantic ignored). They were rewritten as real mutations and are caught. Reviewers also ran their own mutations in scratch copies: round 4: 32 (29 caught, 3 equivalent); round 5: a set whose survivors showed two weak tests (fixed); round 6: 42 (36 caught, 4 survivors: 2 real test gaps now closed, 1 mutant with a bad pattern, 1 equivalent).

## 4. Assumptions and unclear requirements
See BLOCKERS.md (V2 assumptions) and DECISIONS.md (V2-1 ... V2-17). The ones that matter most:
- NUL / undecodable bytes: Strictness vs "contact info never quarantines" (only the email cell is exempt).
- Over-long contact text is truncated + warned instead of quarantined.
- A read with different money still fills contacts (reversed after review).
- `invalid_email` is also recorded for donations that already have an email.
- The follow-up `date` is the donation date.

## 5. What was not finished and why
- Phase B (real Stripe test mode, real Google Sheet) and Phase C (Render): no credentials, out of scope for this task.
- GitHub Actions has not been observed green for these commits.
- Two consecutive clean review rounds (see summary; rounds 5 and 6 each found something).
- Cold start was not repeated after the follow-up session's code changes (the test suite, lint, mypy and a demo run from a copy of the repository without `tests/` were).
- Stripe payments are still quarantined for an implausible `created`, an amount of 0 or above 10^15, or a missing `created` (the spec only exempts currency). Re-drive of Stripe quarantines happens only inside reconcile runs and skips ones a human marked `ignored`.
- Google reader: padding of short rows (which removes truncated-row detection for Google), no timeout beyond the one set in code, and everything else untested.

## 6. If I did it again, which parts of the specification should be clearer
- Say whether "never quarantined because of contact information" overrides the NUL / undecodable-byte Strictness rule for name, address and recipient.
- Say whether a later read whose money differs may still fill contact fields (I first guessed no, a reviewer showed yes follows from "Entry ID is stable and unique").
- Say what to do with over-long contact text, and whether a currency outside ISO 4217 that Stripe reports should be quarantined or stored.
- Say whether warnings may contain digests of contact values.
- Define "date" in the follow-up list (donation date vs. date noticed).
- Say how `donation_contact_changes` should behave when a fill happens outside any run (null run id is assumed).

## 7. Follow-up session (spec change and fixes)

| Item | What changed | How it was verified |
|---|---|---|
| 1. Stripe currency | SPEC.md section 2.3 "Currency" row updated. Stripe: any three ASCII letters, uppercased, never quarantined for currency (`xyz`, `qqq`, `zzz`, `hrk`, ... are stored). Sheet keeps the ISO 4217 whitelist. Non-code values (wrong length, digits, non-ASCII, not text) are still quarantined: the DB CHECK cannot store them. DECISIONS V2-18 | Automated tests for mapper, webhook and reconcile; one existing test changed because the spec changed (recorded); mutations `stripe_currency_whitelist_restored`, `stripe_currency_accepts_non_letters`, `sheet_currency_whitelist_removed` are caught |
| 2. Review rounds | Round 5 and 6 (above). Fixes: reconcile re-drives open Stripe quarantines (so the currency change reaches payments quarantined by v1) and closes stale ones beside an existing donation; tests for warning rollback at the end of a batch, default `SHEET_TIMEZONE`, and the per-payment locks in reconcile | Automated tests; mutations added for each |
| 3. break_it.py | Stale-mutation check (exit 2), refreshed mutation, 43 mutations | Full run: 43/43 caught |
| 4. Report + demo | Mutation count corrected (above). `make demo` no longer needs `tests/`: signing helper moved to `donation_ledger/stripe_signing.py`; `tests/helpers.py` keeps an independent copy | `tests/test_demo_independence.py` (signers equal and accepted by the Stripe SDK; no `tests` import inside the package; demo runs in a copy of the repository without `tests/`) |

Not verified in a real environment (unchanged): real Stripe, real Google Sheets, Render, GitHub Actions.
