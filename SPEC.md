# SPEC: donation-ledger

This is the complete specification for the agent. Read it in full before you start working. Code, comments, commit messages, and the README must all be written in English.

---

## 1. Task and success criteria

**Task:** Build `donation-ledger` from scratch in this empty repository. It is a backend service that writes donations from two sources (Stripe webhooks and a Google Sheet) into one database, keeping the data complete, free of duplicates, and clean, and recovering safely when any step fails.

**Context in one sentence:** This is the donation-management backend for a nonprofit, maintained by a volunteer engineer, so it must be simple, reliable, and easy to take over.

**Priorities:** Correctness over recoverability, over readability and documentation, over feature scope. Never sacrifice correctness to add features.

**Definition of done (all items must be satisfied):**

1. Every requirement in section 2 has a corresponding automated test, and all tests pass.
2. CI is green in a clean environment, including a real PostgreSQL.
3. The offline demo script runs from scratch.
4. `README.md`, `DECISIONS.md`, and `EXPERIMENT_REPORT.md` are complete and consistent with the code.
5. From a fresh clone, following only the README, the tests pass.
6. You have completed the adversarial self-review in section 5 and found nothing new in two consecutive rounds.

**Important:**

- Your delivery will be evaluated against additional scenarios you have not seen. Do not only satisfy the list above; actively think about failure cases.
- If something is not finished, say so clearly. Do not pretend it is done. An honest report matters far more than a polished one.

---

## 2. Business context and requirements

### 2.1 Scenario

A nonprofit's website accepts donations through Stripe. In addition, staff manually enter other donations (in person, checks, and so on) into a Google Sheet. The fields of the two sources are not identical. The system must write donations from both sources into one database: the Stripe side in real time, and the Sheet side through a daily scheduled sync of new entries. Under network problems, scheduled-job failures, and duplicate deliveries, the data must still be complete, free of duplicates, and clean.

### 2.2 Fields of the two sources

| Source | Fields it has | Fields it lacks |
| --- | --- | --- |
| Stripe | Payment ID (starts with `pi_`), amount (smallest currency unit), currency, creation time (Unix timestamp), donor name and email (may or may not be present) | Address, recipient |
| Google Sheet | `Entry ID`, `Date`, `Donor Name` (may be blank), `Donor Address`, `Amount`, `Currency` (may be blank, defaults to USD), `Recipient` | Email, payment ID |

Consult the official Stripe and Google documentation to confirm the real structure of Stripe event objects and the signature algorithm. Do not guess from memory.

### 2.3 Decisions already made (do not re-decide)

These are the places that are easy to leave ambiguous, and they have been decided explicitly. If you think a rule is wrong, follow it anyway and write your reasoning in `DECISIONS.md`.

| Topic | Rule |
| --- | --- |
| Unified table | One `donations` table that both sources write to. It must include `source`, `external_id`, donor name, email, address, recipient, amount, currency, donation time, `is_anonymous`, `raw_payload`, and `created_at` |
| Deduplication | Add a database unique constraint on `(source, external_id)`. Do not rely only on "check, then insert", because that has a concurrency race |
| Stripe external ID | Use the payment intent ID (`pi_...`). However many events one donation triggers, there is exactly one record |
| Sheet external ID | The `Entry ID` column, assumed stable and unique. Rows whose `Entry ID` is empty or duplicated are always quarantined and not written to `donations` |
| Amount | Store as an integer in the smallest currency unit (US cents). Never use floating point. Sheet amounts may look like `1000`, `1,000.50`, or `$1,000.50`; all must be parsed. Rows that cannot be parsed, or are zero or negative, are quarantined |
| Currency | Uppercase ISO 4217 code. If the Sheet leaves it blank, default to USD. No currency conversion |
| Time | Store UTC in the database. Interpret Sheet dates using the environment variable `SHEET_TIMEZONE` (default `America/Los_Angeles`) |
| Anonymous | If the Sheet's `Donor Name` is blank or is "anonymous" (case-insensitive), or Stripe has no name, store `donor_name = NULL` and `is_anonymous = true`. The word "Anonymous" appears only in the presentation layer and is never stored in the database |
| Duplicate delivery | The same event or the same row delivered any number of times gives exactly the same result as delivering it once, and the webhook still returns 2xx |
| Synced record changed upstream | The ledger is append-only. If a row already synced from the Sheet is later edited, do not modify the database; only record a "changed after sync" warning (in `sync_issues`) |
| Rows deleted from the Sheet | Ignore them. Do not delete database records |
| Refunds and disputes | Out of scope. For unrecognized Stripe event types, return 2xx and log them; do not write to any table and do not raise an error |
| Quarantined rows | Write them to a `rejected_rows` table: source, external ID (if any), the raw row, the reason, and the sync run ID. They are retried on the next sync until they pass or are manually marked as ignored. A quarantined row must never be skipped forever because of a "last synced position" |

### 2.4 Functional requirements

**Database:**

- Tables: `donations`, `sync_runs`, `rejected_rows`, `sync_issues`. Manage migrations with Alembic.
- `sync_runs` contains at least: ID, source, start time, end time, status (running, succeeded, failed, partial), rows read, rows inserted, duplicate rows skipped, rows quarantined, error message.

**Stripe webhook:** `POST /webhooks/stripe`

- Verify the signature first. Return 400 and write nothing if the signature is wrong or missing.
- Handle the `payment_intent.succeeded` event.
- Return 2xx when the write succeeds or the record already exists. Return 5xx when the database write fails, so that Stripe retries automatically.
- You must verify the signature against the raw request body. Do not parse and re-serialize first.

**Stripe reconciliation job:** Periodically pull recent payment intents through the Stripe API and add any that the webhook missed. Define an interface for it that can be tested. Tests must not call Stripe for real.

**Google Sheet sync:** Command-line entry point `python -m donation_ledger.sync`, triggered by an external scheduler.

- Each batch is written inside one database transaction, and the whole batch rolls back on failure.
- Prevent two sync jobs from running at the same time (a lock or a lock record).
- A rerun must be correct at any time. Either a "last synced position" or a "full comparison every run" is acceptable. You choose, but explain the trade-off in `DECISIONS.md`.
- Put the code that reads the Sheet behind an interface. Provide a CSV implementation for offline use and tests, and a real Google Sheets implementation (without credentials it only needs to be written, not verified, and must be clearly marked "unverified").

**Read-only API:** `GET /donations` (paginated, filterable by source, anonymous donors shown as Anonymous), `GET /sync-runs`, `GET /health`.

**Observability:** Structured (JSON) logs. Do not log full donor personal information or any secret.

---

## 3. Technical and engineering constraints

**Tech stack (fixed, do not change):**

- Python 3.12 or newer. FastAPI. SQLAlchemy 2.x (typed-annotation style models). Alembic. Pydantic. pytest.
- The database is PostgreSQL. You may use SQLite locally for speed, but **all tests and CI must pass on a real PostgreSQL**, because unique constraints, concurrency, and transactions behave differently from SQLite.
- Use the official Stripe Python SDK. Use `gspread` or the official client for Google Sheets.
- Pin dependency versions and keep dependencies few. Justify every added dependency in `DECISIONS.md`.
- Do not introduce queue systems, message brokers, caches, or frameworks beyond the ORM. This is a simple project, and simple is the requirement.

**Testing:**

- Tests must not connect to the real Stripe or Google; use fakes only at those two external boundaries. **Do not mock the logic under test.** For example, do not mock the database to test deduplication; use a real database.
- Concurrent-deduplication tests must use real multi-threaded or multi-process concurrent writes.
- Fault-injection tests: deliberately raise an error midway through a sync, simulate a dropped database connection, then rerun and verify the final data is correct.
- Every test name must say what behavior it verifies.
- **Never delete a test, weaken an assertion, or hardcode a result to make a test pass.** When a test fails, fix the code, unless the test's own expectation is wrong, in which case say so in `DECISIONS.md`.

**CI:** GitHub Actions with a PostgreSQL service container, running the tests, lint (ruff), and type checking.

**Security:**

- Read secrets only from environment variables. Provide `.env.example` and add `.env` to `.gitignore`. **No secret may be committed to the repository, and even fake secrets used in tests must be clearly marked as fake.**
- The webhook must verify signatures.
- Logs and error messages must not contain secrets or full personal information.
- Check dependencies for known vulnerabilities.

**Code style:**

- Full type annotations. Small, clear functions. Keep business logic separate from the framework and external services.
- Comments explain "why", not "what".
- Code, comments, commit messages, and the README are all in English.

---

## 4. Resources and permissions

The work has three phases. **Phase A needs no credentials and must be fully completed.** Phases B and C are done only when the corresponding resources are provided.

| Phase | Content | Resources needed |
| --- | --- | --- |
| A: offline complete version | All features, all tests, CI, documentation, offline demo | Only a runtime environment and push access to the designated repository |
| B: real integration | Verify once against Stripe test mode and a real Google Sheet | Stripe test key and webhook signing secret; a Google service account and a sample spreadsheet |
| C: deployment | Deploy to Render and run the scheduled job for real | Access to a Render account |

### What you can do yourself

- Install Python dependencies, run a local PostgreSQL (by any available means), and run tests.
- Read public documentation (Stripe, Google, FastAPI, SQLAlchemy, PostgreSQL, GitHub Actions).
- Create branches, commit, and push in the designated repository.

### Offline substitutes when no credentials are available

- **Stripe:** Use the signature algorithm in the official documentation to generate fake events with a valid `Stripe-Signature` (format: `t=<timestamp>,v1=<HMAC-SHA256(secret, "<timestamp>.<raw request body>")>`; the official documentation is authoritative), and verify them with the SDK's `Webhook.construct_event`. This lets signature verification be tested fully offline.
- **Stripe reconciliation:** Put "fetch recent payment intents" behind an interface and test it with a fake. Without credentials, the real implementation only needs to be written and marked "not verified in a real environment".
- **Google Sheet:** Implement the same interface with a CSV file. Provide a sample CSV with a variety of bad data (blank names, inconsistent amount formats, duplicate `Entry ID`s, missing `Entry ID`s, negative amounts, and so on).
- **Deployment:** Only the deployment configuration and instructions need to be written; no actual deployment is needed.

### What not to do

- Do not try to register accounts, obtain credentials, or bypass authentication.
- **Never use any Stripe live key.** Only test keys starting with `sk_test_` are allowed. If you find a key starting with `sk_live_` in the environment, stop immediately and write it to `BLOCKERS.md`.
- Do not access any service other than package registries and official documentation.
- Do not modify any file or repository outside the designated repository.

### Verification must be honest

In `EXPERIMENT_REPORT.md`, list the verification status of every feature in one table: **verified by automated tests, verified in a real environment, or code written but not verified**. Anything that was not run in a real environment must not be reported as verified.

---

## 5. Autonomous working protocol

Work as independently as you can across the whole project, and do not stop to ask about small matters. This is how you work.

### 5.1 Working loop

1. **Plan first.** Read all of `SPEC.md` and write `PLAN.md`: milestones, the acceptance check for each milestone, the risks you foresee, and the list of tests to write.
2. **Test first, in small steps.** For each feature, write a failing test first, confirm it fails for the right reason, and then write the implementation.
3. **Run the full test suite after every change.** Commit only when green. Keep commits small and frequent, with English commit messages.
4. **At the end of each milestone,** update `PROGRESS.md` (what was done, test results, next step, known issues).
5. **When the features are done, begin the adversarial self-review** (see 5.4).
6. **Finish with a cold-start check:** a fresh clone, following only the README, must pass the tests and run the demo.

### 5.2 Decision rights

**You may decide on your own:** choice of libraries (within the constraints in section 3), file and module structure, naming, table details, test strategy, and implementation choices such as "high-water mark versus full comparison".

**Record every decision in `DECISIONS.md`:** the context, the options considered, what you chose, why, and how easy it is to change later.

**In the following situations, stop, write it to `BLOCKERS.md`, and continue with the parts that are not affected:**

- You need credentials, accounts, or paid services that you do not have.
- The requirements contradict each other, and it affects money or data correctness.
- You would need to delete or overwrite something outside the repository.
- Anything that involves real money or a live environment.

If nobody can answer immediately, continue with the safest, most conservative assumption and mark it as an assumption in `BLOCKERS.md`.

### 5.3 Prohibited actions

- Do not delete or weaken a test to make it pass.
- Do not hardcode results to satisfy a test.
- Do not mock the logic under test.
- Do not commit any secret.
- Do not force push, and do not rewrite history that has been pushed.
- Do not report a result you did not actually run. "I believe it works" is not the same as "I ran it".
- Do not ignore edge cases just to look finished.

### 5.4 Adversarial self-review

You are prone to bias about what you just wrote, so look for faults on purpose. When the features are done, run at least two rounds:

1. **Have an independent review subagent review the work.** It sees only `SPEC.md` and the code, not your reasoning, and its goal is to find paths that cause duplicate, lost, or wrong data, or a hang.
2. **Write tests that deliberately try to break your own work.** For example: the same event delivered concurrently, a sync process killed halfway, odd amount formats, empty values and very long strings, duplicate `Entry ID`s, timezone boundaries, renamed spreadsheet columns, Stripe events arriving out of order, and one donation triggering several events.
3. Fix every problem you find, turn it into a test, and record it in `EXPERIMENT_REPORT.md`.
4. Repeat until **two consecutive rounds find nothing new**.

### 5.5 Budget and stopping conditions

- The maximum working time and token limit are given in the launch prompt.
- **Normal stop:** every item in the definition of done in section 1 is satisfied.
- **Stop when the budget is used up:** deliver the current state and state clearly in the report what is still missing.
- **Stop when stuck:** if you cannot resolve the same problem after five attempts in a row, write it to `BLOCKERS.md` and move on to other parts.

### 5.6 Process files

`PLAN.md`, `PROGRESS.md`, `DECISIONS.md`, `BLOCKERS.md`, `EXPERIMENT_REPORT.md`. They are not for show. They let someone who did not watch you work take over.

---

## 6. Deliverables

**The repository must contain:**

| Deliverable | Requirement |
| --- | --- |
| Source code | Clear modules, complete type annotations, business logic separate from external services |
| Database migrations | Alembic; a single command creates the schema from an empty database |
| Tests | Every requirement in section 2 has a test, including concurrent deduplication and fault injection |
| CI | GitHub Actions with a PostgreSQL service container, green |
| `README.md` (English) | See the structure below |
| `DECISIONS.md` | Context, options, and reasoning for every important decision |
| `PLAN.md`, `PROGRESS.md`, `BLOCKERS.md` | Process records |
| `EXPERIMENT_REPORT.md` | See the structure below |
| Offline demo | Runs with one command; see below |
| `.env.example`, `.gitignore` | Contain no real secrets |
| Sample data | A CSV with a variety of bad data, and several fake Stripe events |

### README structure

1. What the project is and what problem it solves (three sentences).
2. An architecture diagram (use Mermaid): the two sources, the unified table, and the sync job.
3. Data model: an explanation of each table and each key field.
4. **Correctness and recovery:** one or two paragraphs explaining clearly why duplicates cannot occur and how the system recovers after a failure. This is the most important section.
5. How to run: offline mode and real-integration mode.
6. How to test.
7. Design trade-offs: what you considered and why you chose what you chose.
8. Known limitations and what is out of scope.
9. What to do when something goes wrong (runbook).

### What the offline demo must do

One command (for example `make demo`) that shows the following in order, printing a clear explanation and result at each step:

1. Initialize the database.
2. Send a correctly signed Stripe event, then send the exact same event again; the table has only one record.
3. Send an event with a wrong signature; it is rejected.
4. Run the sync on a CSV containing bad data; good rows are inserted, bad rows are quarantined with a reason.
5. Simulate a failure midway through a sync, then rerun; the data has no duplicates and nothing is missing.
6. Print the final tables and `sync_runs`.

### `EXPERIMENT_REPORT.md` structure

1. A summary table: start and end time, number of iteration rounds used, number of human interventions, total tests and number passing.
2. The verification status of every feature (verified by automated tests, verified in a real environment, or code written but not verified).
3. Problems found by the adversarial self-review and how they were fixed.
4. Assumptions you made and requirements that were unclear.
5. What was not finished and why.
6. If you did it again, which parts of this specification should be written more clearly.
