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
- The launch prompt asks for branch `agent/build`; the harness also designates `claude/fervent-ride-w959iu`. Work is on `agent/build` (explicit instruction in the task). Nothing was force-pushed.

## D13. Test expectation that was wrong (not weakened)
- In `test_error_raised_midway_...` I first asserted `rows_duplicate_skipped == 0` on the rerun. That expectation was wrong: the 20 rows committed before the failure are correctly reported as already-present duplicates on the rerun. The assertion was corrected to 20 (the stronger, accurate statement); no code was changed to fit.

## D14. Clock/timezone for `donated_at` of sheet rows without time
- Midnight in `SHEET_TIMEZONE`. A donation on 31 Dec local evening lands on 1 Jan in UTC; this is correct for storage and is covered by tests.
