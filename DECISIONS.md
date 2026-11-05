# DECISIONS

Format: context, options, choice, why, how easy to change later.

## D1. Sync strategy: full comparison every run (no high-water mark)
- **Options:** (a) remember the last synced row/position; (b) read the whole sheet every run and rely on idempotent inserts.
- **Choice:** (b). **Why:** the spec forbids skipping a quarantined row forever; with (a) a bad row behind the marker would need extra bookkeeping, and a sheet edited above the marker would be missed. (b) is self-healing after any failure. **Cost:** one full sheet read per run (a nonprofit's sheet is thousands of rows, trivial) and O(rows) comparisons. **Change later:** easy; add a marker only if sheets reach hundreds of thousands of rows.

## D2. PostgreSQL only
- Unique constraints, `ON CONFLICT`, JSONB, advisory locks are used directly. The spec requires tests on real PostgreSQL anyway; keeping a SQLite path would mean two behaviours to maintain. **Change later:** hard (deliberate).

## D3. Dedup = `INSERT ... ON CONFLICT DO NOTHING` on `UNIQUE(source, external_id)`
- No pre-check for the Stripe path at all. For the sheet path a pre-`SELECT` is used **only** to compare an existing row for change warnings; the insert itself is still `ON CONFLICT DO NOTHING`, so the race the spec warns about cannot cause duplicates.

## D4. Lock: PostgreSQL session advisory lock (not a lock table)
- **Options:** lock row/table; advisory lock. **Choice:** `pg_try_advisory_lock` on a dedicated connection, one key per job (`sheet`, `stripe_reconcile`). **Why:** it is released by the server when the connection dies, so a SIGKILLed job cannot leave a stuck lock (a lock table would need timeouts/heartbeats). **Safety net:** the lock connection is pinged before every batch and the run aborts if it died; and correctness never depends on the lock (idempotent inserts), it only avoids duplicate work. Stale `running` rows are marked `failed` by the next lock holder (only the holder may do this, so a live run is never touched).
- A run that cannot get the lock records a `failed` row ("another sync is already running") and exits with code 3.

## D5. Batches and failure semantics
- One transaction per batch (default 500, `SYNC_BATCH_SIZE`) containing the donation inserts, rejections, warnings **and** the `sync_runs` counter update. Failure of a batch rolls it back and stops the run: status `partial` if an earlier batch committed, else `failed`. Remaining batches are not attempted because the cause is usually the database itself; rerun is safe.
- `sync_runs` start/finish rows are separate small transactions so a failed batch never rolls back the bookkeeping.

## D6. Quarantine design
- Identity of a rejected row is a SHA-256 fingerprint of its cell contents (not row number, so inserting rows above doesn't create new entries). Unique `(source, fingerprint)`.
- Statuses: `open` (re-evaluated every run), `resolved` (a donation now exists for that ID, or the row vanished/was edited after a fully successful run), `ignored` (manual: silences that rejection only; if the same row later becomes valid, e.g. the other copy of a duplicate ID was deleted, it IS ingested - found by review, suppressing it would lose a donation). A resolved row that reappears is reopened.
- Byte-identical rows in the same sheet get distinct fingerprints (an occurrence counter is mixed in), so they are separate rejected entries and ignoring one does not silence the other.
- A row whose `Entry ID` already exists in `donations` is never quarantined for field problems; it is compared and, if different, produces a `changed_after_sync` warning (append-only rule). Duplicate/empty IDs are always quarantined, including the first copy, even when the ID already exists in the ledger (the existing donation stays untouched).
- Rows with more cells than headers (typically an unquoted comma in an address that shifts every column) are quarantined; fully blank rows are skipped silently.
- Open rejections not seen in a **fully successful** run become `absent` (not `resolved`: nothing was fixed). They reopen if the same row returns, so a truncated read loses nothing. `resolved` is only set when a donation with that ID now exists.
- "Anonymous" is scrubbed to an empty cell inside stored sheet `raw_payload`/`raw_row` too (found by review), and change detection compares the scrubbed form, so re-casing it is not a warning.

## D7. Sheet validation specifics
- Header names matched case-insensitively with whitespace collapsed and BOM removed; order irrelevant; **all seven columns required** (a renamed `Currency` column must not silently default everything to USD). Missing/duplicate column => the run fails and writes nothing.
- Amount regex allows `1000`, `1,000.50`, `$1,000.50`, rejects everything else (negatives, parentheses, `1.000,50`, exponents, other symbols). More decimals than the currency allows is rejected (no rounding of money). Minor-unit exponents: 0 for the Stripe zero-decimal list (JPY, ...), 3 for BHD/KWD-style currencies, else 2. Upper bound 10^15 minor units.
- Currency must be a known ISO 4217 alphabetic code (list embedded); blank => USD.
- Dates: ISO 8601 (with or without time/offset) and US `M/D/YYYY[ H:MM[:SS][ AM/PM]]`. Naive values use `SHEET_TIMEZONE`; ambiguous DST fall-back times take the first occurrence, non-existent spring-forward times follow `zoneinfo`'s default. Plausible range 2000-01-01 to now+2 days (typo protection; a future-dated row is quarantined until its date arrives, then ingested).
- Text with NUL characters is quarantined (PostgreSQL cannot store NUL; one such row would otherwise fail its whole batch on every run forever). Over-long name/address/recipient/ID quarantined. Lone surrogates are replaced.
- Names: whitespace collapsed; `anonymous` (any case) or blank => NULL name. Address/recipient blank => NULL.

## D7b. CSV reader robustness
- Decoded with `errors="replace"` and a 10 MB field limit so one bad byte or giant cell cannot abort the whole file (the cell is stored with U+FFFD or quarantined by length checks). **Not handled:** an unterminated quote swallows the following lines into one cell (inherent to CSV); that row is then quarantined/odd and rows after it are missed until fixed. Google values have no such problem.
- Zero-width/format-only names count as anonymous. ASCII-only currency codes (so `ınr` is not silently "corrected" to INR).

## D7c. Test expectation changed on purpose
- `test_csv_with_a_bad_byte_and_an_oversized_cell...` first expected a Latin-1 byte to be stored as U+FFFD. Review round 5 showed that is permanent corruption (and a later UTF-8 re-export would insert the donation a second time), so such rows are now quarantined (`invalid_text: undecodable characters`). The test was updated to the new, stricter behaviour; this is a behaviour decision, not a weakened assertion.
- `$` is accepted only for dollar/peso-family currencies; headers are matched after stripping invisible characters.

## D7d. Truncated rows
- A row with fewer cells than the header is quarantined (an interrupted CSV export would otherwise be ingested as a smaller donation that the append-only ledger can never fix). The Google reader pads trailing blanks itself because the API trims them. Entry IDs containing invisible characters *inside* the ID (soft hyphen) are not normalised (marginal).

## D7e. Fail-closed configuration
- `SHEET_SOURCE` has no default (a forgotten setting must never ingest the bundled sample CSV into a real ledger). `RECONCILE_LOOKBACK_HOURS` is capped at one year. DB sessions are pinned to `timezone=UTC`, and Stripe `created` must be within 2000-01-01..now+2 days, because one absurd timestamp otherwise made `GET /donations` fail for the page containing it. Client-library (Stripe/Google/HTTP) exceptions are stored by type only (their messages can echo key fragments). A stale bad snapshot of a payment already in the ledger is answered as a duplicate and does not reopen its rejection.

## D7f. Review-7 fixes
- `SHEET_SOURCE` is validated only when the sheet reader is built (the web service, reconcile and admin CLI must start without it; my earlier fail-closed change broke them - found by review and covered by a test that runs `create()` and the sync CLI without it).
- Before the final status update a run row-locks its own `sync_runs` row and aborts if it is no longer `running` (its advisory lock was lost and a newer run already cleaned it up), so a stale run cannot overwrite a live run's bookkeeping. Runs found dead are labelled `partial` when batches had committed (`rows_read > 0`), else `failed`.
- Zero-decimal currencies accept `5000.00` (trailing zeros only). Stripe ids shorter than `pi_x` are rejected. Hangul-filler style invisible characters (not Cf/whitespace) are not detected (rare).

## D7g. Secrets at rest and in config
- PaymentIntent `client_secret` (any key containing it) is dropped before storing/fingerprinting. `Settings.__repr__` hides secrets. `.env.example` placeholder values (`replace_me`/`replace-me`) are refused as real secrets, and `SHEET_SOURCE` is commented out there. `Settings.from_env` validates all variables for every entry point (a bad value in a shared env group stops the web service too): chosen for fail-fast simplicity; give each service only the variables it needs.
- render.yaml pins `PYTHON_VERSION` (code needs 3.12+); cron services have no pre-deploy migration step, so run `alembic upgrade head` via the web service deploy first.

## D7h. Round 9a
- An edit that turns a synced entry into a rejected row (duplicate ID, short/long row, bad bytes) records a `changed_after_sync` issue too. Entry IDs are NFC-normalised. A fraction of 3+ digits in a non-zero-decimal currency (`1.000` EUR) is quarantined, since it may be a thousands separator; only `5000.00` for zero-decimal currencies is tolerated. ISK/UGX follow Stripe (2 decimals), not ISO 4217. Addresses are stored whitespace-collapsed (original text stays in `raw_payload.fields`).

## D7i. Round 9b
- Webhook and reconcile take a transaction-scoped advisory lock per Stripe payment ID (sorted order) before reading/writing, so a bad and a good snapshot of one payment can't leave an open rejection beside its donation (396/400 race outcomes before; 0 after, covered by a test that fails with the lock disabled). `finish_run` only updates rows still `running`, so an ambiguous COMMIT cannot overwrite a recorded success.

## D7j. Round 10
- docker-compose binds Postgres to 127.0.0.1; `.env.*` is git-ignored; `constraints.txt` pins the transitive dependency tree (used by CI/`make install`; `pip-audit` clean). Lone surrogates in Google-sourced text are replaced with `?` (practically unreachable; the CSV path cannot produce them).

## D7k. Round 12
- A NUL inside a PaymentIntent id quarantines the event (stripping it could alias another payment). httpx is pinned at 0.28.1 and Starlette emits a deprecation warning suggesting `httpx2` for its TestClient: harmless today, revisit on upgrade.

## D8. Stripe mapping
- ID = `data.object.id` (`pi_...`). Amount = `amount_received` if positive, else `amount` (the money actually captured is the truthful figure; they differ on partial capture). `created` Unix seconds => UTC.
- A PaymentIntent has no donor name; read from (expanded) `latest_charge.billing_details` / legacy `charges.data[0]`, then `metadata.donor_name|name`, `shipping.name`; e-mail from billing details, `receipt_email`, `metadata.donor_email|email`. **Assumption** (documented in BLOCKERS.md): the website puts donor info in one of these places. No name => anonymous.
- Signed but unusable `payment_intent.succeeded` (zero amount, non-`pi_` ID, missing fields) => quarantined in `rejected_rows` and **200** returned. Returning 4xx would make Stripe retry something that can never succeed; silently dropping would lose evidence.
- Signature is verified with the SDK's `Webhook.construct_event` on the raw bytes (default 300 s tolerance), then the same bytes are parsed with `json.loads` (so our own plain dicts are stored, not SDK objects). Missing secret => 503, nothing written. Body > 1 MB => 413.
- Event types other than `payment_intent.succeeded` => 200, one log line, no writes.

## D9. Read-only API
- Requires `Authorization: Bearer $API_TOKEN` (constant-time compare); fails closed (503) when no token is configured unless `ALLOW_UNAUTHENTICATED_API=true` (demo/local). `/health` is open. **Reason:** the spec says nothing about auth but the data is donor data; the safest assumption wins.
- `/donations` exposes neither e-mail, address, nor `raw_payload`. Keyset pagination (`before_id`, newest first) instead of offset so concurrent inserts cannot cause skips/repeats.

## D10. Errors and logs
- `sync_runs.error_message` and logs contain exception type plus the first line of the driver message only. SQLAlchemy's default messages embed bound parameters (donor data), so they are never stored or logged. The JSON formatter drops tracebacks (logs `exc_type`). Log calls pass IDs and counts only.

## D10b. Timeouts
- DB: connect 10 s, statement 120 s, lock 30 s. Google client 60 s. Without them a stalled network could hold the sync advisory lock forever and block every later run (found by review).

## D11. Dependencies (all pinned in `requirements*.txt`)
fastapi + uvicorn (spec), SQLAlchemy 2 + alembic + psycopg 3 (spec; psycopg binary wheel avoids system libpq), pydantic (response models; already a FastAPI dependency), stripe (spec: signature verification and API client), gspread (spec). Dev: pytest, httpx (needed by FastAPI's TestClient), ruff, mypy, pip-audit. No pydantic-settings: settings are a 40-line dataclass over `os.environ`. `pip-audit` reported no known vulnerabilities for the runtime pins when run (see report).

## D12. Branch name
- The launch prompt asks for branch `agent/build` (the hosting environment also suggested a different branch name, `claude/fervent-ride-w959iu`). Work is on `agent/build` (explicit instruction in the task). Nothing was force-pushed.

## D13. Test expectation that was wrong (not weakened)
- In `test_error_raised_midway_...` I first asserted `rows_duplicate_skipped == 0` on the rerun. That expectation was wrong: the 20 rows committed before the failure are correctly reported as already-present duplicates on the rerun. The assertion was corrected to 20 (the stronger, accurate statement); no code was changed to fit.

## D14. Clock/timezone for `donated_at` of sheet rows without time
- Midnight in `SHEET_TIMEZONE`. A donation on 31 Dec local evening lands on 1 Jan in UTC; this is correct for storage and is covered by tests.

---

# Version 2 decisions

Everything below is new in version 2 (SPEC.md v2). Decisions D1-D14 above still stand unless a V2 entry says otherwise.

## V2-1. Where contact policy lives: one function, three callers
- **Context:** three paths can see a donation again (webhook duplicate, reconciliation, sheet sync). Implementing the rules three times would let them drift.
- **Choice:** `contacts.apply_reads` is called by all three right after `insert_donations`, inside the caller's transaction. It loads the stored rows, warns about money differences, fills empty contact fields with `UPDATE ... WHERE id=:id AND <field> IS NULL`, writes the audit row only when that UPDATE changed a row, and records warnings. **Change later:** easy.
- **Fill race:** the loser of a race gets 0 rows from its UPDATE, re-reads the stored value and records `contact_changed_after_sync` only if it differs from what it wanted to write (test: 8 threads, different values: 1 audit row, 7 warnings; same values: 1 audit row, 0 warnings).
- **Name:** filling `donor_name` sets `is_anonymous = false` in the same UPDATE (the CHECK `is_anonymous = (donor_name IS NULL)` would reject a two-step update).

## V2-2. Warning details never hold contact values
- `sync_issues.detail` for contact warnings holds the field name and a 16-hex-digit digest of the new value (so one distinct value is recorded once, not on every run), never the email/name/address. Money warnings hold the money values (not personal). `donation_contact_changes` has no value column by design.

## V2-3. A read whose money differs still fills empty contact fields (changed after review round 1)
- **History:** the first implementation refused to fill contacts from a read whose amount/currency/date differed (fear that a reused Entry ID is a different donation). The independent review pointed out that the spec says the Entry ID is stable and unique, so a later read with the same ID *is* the same donation, and that the rule left staff-corrected rows on the follow-up list forever.
- **Choice:** money differences produce the `changed_after_sync` warning and never change money; contact fields are filled regardless. **Risk accepted:** if someone really reuses an Entry ID for another donor, that donor's empty contact cells could complete the old row; `contact_changed_after_sync`/`changed_after_sync` warnings and the audit table make it visible. Tests: `test_a_row_whose_amount_was_edited_still_fills_contacts_but_never_changes_money`, reconcile equivalent.

## V2-4. Money differences: Stripe by column, sheet by cell text
- Stripe and sheet rows are both compared by **parsed value** (`amount_minor`, `currency`, `donated_at`) with the stored columns, so reformatting (`1000` -> `$1,000.00`, `2024-03-01` -> `3/1/2024`) is not a change (review round 1; v1 compared raw text). `changed_fields` therefore now names `amount_minor` / `currency` / `donated_at`. Only a sheet row that no longer parses at all falls back to the v1 raw-text comparison. Contact cells are excluded from that text comparison and compared with the **columns** instead, because rows stored by version 1 have no `donor_email` key in `raw_payload` (a raw-text diff would raise a false warning for every old row after the upgrade; test: `test_version_2_sync_on_an_upgraded_version_1_database...`).
- `raw_payload` is never modified (it is the original input); filled values live only in the columns.

## V2-5. Contact information never blocks a donation; where that meets the Strictness rule (ASSUMPTION)
- **Contact problems that no longer quarantine a sheet row:** missing/malformed email (stored NULL + `invalid_email`), over-long donor name / address / recipient (cut to 500/2000/500 characters, `rstrip`ped, plus a `contact_truncated` warning; the full text stays in `raw_payload`). A truncated value is deterministic, so a re-read compares equal and fills/warnings do not flap.
- **Conflict in the spec:** section 2.3 "Strictness" says NUL characters and undecodable bytes are quarantined; section 2.3 "Donor email" says contact problems never quarantine. **Resolution:** the **Donor Email cell is exempt** from the NUL / undecodable-byte checks (a bad byte there gives `invalid_email`, never a quarantine); NUL or undecodable bytes in any other cell (including name/address/recipient) still quarantine the row, because they indicate a wrongly encoded file, and a re-export in the right encoding would otherwise insert the donation twice. This is the narrowest reading that satisfies both sentences. **Flagged in BLOCKERS.md as an assumption.**
- Entry ID, date, amount, currency problems quarantine exactly as before.

## V2-6. Email validation details
- Trim, `lower()`, max 254 characters (checked after trimming), exactly one `@`, non-empty local part, domain with a dot that does not start/end with a dot or contain `..`, no whitespace / control / format characters, no U+FFFD. These last few go slightly beyond the spec's "simple check" but only reject text that cannot be an address. Non-string values (a number in `receipt_email`) count as malformed. A blank or whitespace-only value is "missing", not "invalid".
- **Stripe order:** `receipt_email`, then `metadata.donor_email`. The first *valid* candidate wins, so a malformed `receipt_email` does not hide a valid metadata email; if none is valid and one was non-blank, `invalid_email` is recorded.
- `invalid_email` is recorded on every read that sees a malformed value (de-duplicated by digest), also for donations that already have an email; the follow-up list only looks at donations with a NULL email.

## V2-7. Stripe donor details (SPEC 2.3)
- Name = `metadata.donor_name`; email = `receipt_email`, else `metadata.donor_email`. Removed: `latest_charge.billing_details`, legacy `charges.data[0]`, `shipping.name`, `metadata.name/email`. The real Stripe listing client no longer asks for `expand=data.latest_charge`. Reading the Charge's billing details is a possible future extension (needs one more API call per payment or an expanded event).
- Stripe donations never carry an address or recipient.
- A name longer than 500 characters is cut and flagged like the sheet's.

## V2-8. The money trigger and what it does not do
- Migration `0002` adds `donations_reject_money_update()` (BEFORE UPDATE, per row): any UPDATE that changes `amount_minor`, `currency`, `donated_at`, `source` or `external_id` raises (`check_violation`, message "money fields of donations are immutable"). Setting a column to its current value is allowed. Tests update each column through plain SQL and through a second session.
- **Not blocked:** DELETE, TRUNCATE, `id`, `created_at`, `raw_payload`, and contact columns. Contact immutability beyond fill-once is application-level only, on purpose: a human who must correct a wrong email can do it with an ordinary UPDATE after reading the `contact_changed_after_sync` warning. A table owner can drop the trigger; that is a deliberate act, not a code bug. **Change later:** add a contact trigger if the volunteer wants it enforced there too.

## V2-9. Migration 0002 and existing data
- The initial migration is untouched. `0002` adds the `donation_contact_changes` table, `sync_runs.contacts_filled` (default 0) and the trigger; it does not rewrite any existing row. `donations.donor_email` already existed (NULL) in `0001`, so v1 rows simply keep NULL and appear in `/followups` as `missing_email` until a later read fills them. Downgrade removes the trigger, table and column.
- Test database: tests upgrade a scratch database created at revision `0001` and populated with rows, then compare every `donations` row before and after (needs a PostgreSQL role that may `CREATE DATABASE`, as the docker-compose/CI `ledger` user is).

## V2-10. `GET /followups`
- Same auth and keyset pagination as `/donations` (`limit` 1-200, `before_id`, newest first, optional `source`). Items: `donation_id, source, external_id, date, reason`; nothing else. `reason` is `invalid_email` when an `invalid_email` warning exists for that donation, otherwise `missing_email`. The `date` is the donation time (`donated_at`).

## V2-11. `sync_runs.contacts_filled`
- New counter (also in `GET /sync-runs`) so an operator sees at a glance how many fills a run made. Added in the same transaction as the batch like the other counters.

## V2-12. Existing tests that had to change (each one recorded; no assertion weakened beyond the spec change)
| Test | Change | Reason |
|---|---|---|
| `tests/test_sync.py` `HEADER`, `good()`, and literal rows in `test_sync.py`, `test_adversarial.py` | 8 columns: an empty `Donor Email` cell inserted after `Donor Name` | sheet now has eight columns |
| `test_sample_csv_inserts_good_rows...` | expects the new sample (16 rows read, 7 inserted, 9 quarantined; S-015 added), adds asserts on emails and `invalid_email` warnings | sample CSV has an email column and one more good row |
| `test_rerunning_the_same_sheet...` | 7 duplicates instead of 6; adds `issues_recorded == 0` | same |
| `test_header_matching_ignores_case_spacing_bom_and_column_order` | header and row gain a `Donor Email` column; asserts the email | header must contain all eight columns |
| `test_very_long_values_are_quarantined_or_stored_without_error` (renamed `test_over_long_contact_text_is_truncated_and_over_long_ids_are_quarantined`) | the 100,000-character *name* is now stored cut to 500 characters instead of quarantined; the over-long Entry ID is still quarantined | contact info never blocks a donation (V2-5) |
| `test_csv_with_a_bad_byte_and_an_oversized_cell_still_syncs_the_other_rows` | oversized name row C2 is stored (cut, with a `contact_truncated` warning) instead of quarantined; the undecodable-byte row C1 is still quarantined | same |
| `test_duplicate_extra_header_names_keep_both_columns` | second duplicate extra header is `Note_10` instead of `Note_9` | the extra column moved one position right |
| `test_sync_runs_endpoint...` | 7 inserted instead of 6 | new sample row |
| `test_name_and_email_are_read_from_charge_billing_details` (renamed `..._from_metadata_by_the_webhook_end_to_end`), the anonymous-name test in `test_adversarial.py`, the PII-log test in `test_api.py` | donor name now comes from `metadata.donor_name` | Stripe donor details rule (billing details out of scope) |
| `test_migration_creates_the_four_required_tables` (renamed ...`all_five...`) | also expects `donation_contact_changes` | new table |
| `tests/conftest.py` | TRUNCATE list includes `donation_contact_changes` | new table |
`tests/test_parsing.py::test_overlong_names_are_rejected` is unchanged: `normalize_donor_name` still raises; the sheet code cuts the text first.

## V2-13. New dependencies
- None.

## V2-14. Break-it harness in the repository
- `scripts/break_it.py` applies each mutation listed in it (weaken one safety property), runs the whole suite, restores the file, and reports whether a test failed. It is a developer tool, not part of CI (it takes several minutes and rewrites source files while it runs).

## V2-15. Review round 1 outcomes
- Currency whitelist: `SLL, STD, MRO, BYR, XCG, ZWG` added (Stripe can report them; a payment must not be quarantined for its currency). Still a whitelist: unknown codes such as `XYZ` stay quarantined. Residual risk: a future Stripe currency missing from the list is quarantined (visible in `rejected_rows`) until the list is extended.
- Not changed: a missing/oversized unsigned request gets 413/503 before the 400 check (nothing is written either way). Contact digests in `sync_issues.detail` are unsalted 16-hex SHA-256 prefixes of values that are stored in plain text in `donations` anyway. The Google reader pads short rows (documented, unverified).
- `test_stripe_mapping.py` change: `changed_fields` assertion in `test_edit_after_sync_only_records_a_warning...` now expects `["amount_minor"]` instead of `["amount"]` (parsed comparison).

## V2-16. Review round 2 outcomes
- **Fixed:** a lone surrogate in an email is now invalid (category `Cs`), so one such sheet cell can no longer make every INSERT fail; Stripe emails are validated on the original value, not after `sanitize` (stripping NUL/surrogates could have turned a malformed address into a different valid one). Tests in `test_parsing.py`, `test_stripe_mapping.py`, `test_sync_contacts.py`.
- **Accepted, not changed:** (a) ignoring one of two byte-identical bad rows and then deleting the *first* copy makes the survivor take over occurrence 0 and inherit the ignore (identity by content + occurrence number cannot distinguish them; a human ignored "that content" and it is still the same content). (b) `Anon<invisible>ymous` inside the word is stored as typed: cosmetic, would need a stronger normaliser. (c) Unsalted 16-hex digests in `sync_issues.detail` (see V2-15). (d) A real Stripe currency missing from the whitelist is still quarantined (see V2-15). (e) A CSV cell over 10 MB fails the CSV run (v1 limitation, documented).

## V2-17. Review round 3 outcomes
- Added a test for a non-existent spring-forward wall time (deterministic, asserted UTC value).
- **Accepted, cosmetic:** when two rows share an Entry ID (both quarantined) and one is removed, inserting the remaining row marks *both* entries `resolved` (a donation now exists for that ID) instead of leaving the vanished row's entry `absent`. We cannot tell a vanished row from an edited-and-fixed row by ID alone, and `resolved` for the edited case is the v1 behaviour. No money or contact data is affected.
- **Re-confirmed assumption:** NUL / undecodable bytes in name, address or recipient still quarantine the row (SPEC "Strictness"), while the same in the Donor Email cell does not (BLOCKERS V2 assumption 1). A reviewer noted that a literal reading of "contact info never quarantines" would exempt all contact cells; if you prefer that, change `checked` in `sheet_rows._classify_row` and store NULL + a warning.

## V2-18. Follow-up 1: Stripe currency is never a reason to quarantine (SPEC change)
- **SPEC change:** the "Currency" row of the decisions table (section 2.3) now says any three-letter alphabetic code reported by a PaymentIntent is stored in uppercase; the ISO 4217 whitelist stays for the Google Sheet only.
- **Implementation:** `stripe_mapping._currency` accepts exactly three ASCII letters (after trimming) and uppercases them; `parsing.normalize_currency` (sheet) is unchanged. A value that cannot be a code at all (wrong length, digits, non-ASCII letters, not a string, missing) is still quarantined because the database CHECK `^[A-Z]{3}$` cannot store it; this is the only case left and it cannot be fixed by guessing. This **supersedes** the "whitelist plus a few extra codes" compromise of V2-15 / round 1 for Stripe (the extra codes SLL, STD, MRO, BYR, XCG, ZWG remain in the list, now only relevant to the sheet).
- **Existing test changed because the spec changed:** `test_stripe_payload_never_stores_the_word_anonymous_and_unknown_currency_is_quarantined` (renamed `..._an_unlisted_currency_is_still_stored`) used to expect `pi_XYZ` with currency `xyz` in `rejected_rows`; it now expects a donation with currency `XYZ` and an empty `rejected_rows`. The anonymous-name assertions are unchanged. The round-1 test `test_any_three_letter_stripe_currency_is_stored...` was widened (unlisted codes `xyz`, `ZZZ`, `qqq`).
- **Consequence to know:** a typo-like code from Stripe is stored as is; `amount_minor` uses the Stripe smallest unit as reported, so for a currency with a non-2 decimal exponent the value is whatever Stripe sent (no conversion is done for Stripe).

## V2-19. Review round 5 outcomes
- **Reconcile re-drives open Stripe quarantines.** A payment quarantined by an older, stricter rule (the v1 currency whitelist) is older than the lookback window, so listing would never see it again; the currency change would then only apply to new payments. `reconcile` now re-maps every open Stripe rejection (`rejected_rows.raw_row`) under the current rules at the start of each run, inserts the ones that now map cleanly (resolving their rejection, with fills/warnings/counters in the same batch transaction) and leaves the others untouched (no new row, no attempt increment). `ignored` and sheet rejections are never touched. This needs a reconcile run (Stripe key) after upgrading; there is no separate command.
- **Two weak tests strengthened (no code change):** a fault injected at the very end of a batch (counter update) now proves that warnings, fills, audit rows and quarantine records are all rolled back (the earlier fault point failed before any warning was written); the default `SHEET_TIMEZONE` is asserted.

## V2-20. Break-it harness hardening (follow-up 3)
- The mutation `stripe_ignores_metadata_email_fallback` had gone stale when `_donor` started reading the raw payload (round 2): the harness had last been run before that change, so it was not "silently skipped" by the script (it printed a warning and returned 1) but I did not re-run it. Now: before anything runs, **every** target text (and each extra edit) must match exactly once, otherwise the script prints the stale names and exits with status 2 without running anything; a test (`tests/test_break_it_script.py`) performs the same check on every test run, so a stale mutation fails CI; the unmutated suite must be green first (exit 2 otherwise); a pytest crash counts as caught; the harness's own test is excluded from mutation runs (it would fail under any mutation); `--maxfail=1` stops at the first failing test per mutation.

## V2-21. The demo no longer needs `tests/` (follow-up 4)
- `donation_ledger/stripe_signing.sign_payload` (the documented HMAC scheme) is used by `demo.py`. `tests/helpers.sign` stays as an independent copy so tests do not depend on code they check; `tests/test_demo_independence.py` compares both signers, verifies the output with the Stripe SDK, forbids `tests` imports inside the package, and runs the demo from a copy of the repository that has no `tests/` directory.
