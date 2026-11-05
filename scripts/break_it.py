"""Break the code on purpose (SPEC 5.4 step 3): for each safety property, weaken the code that
provides it, run the test suite, and report which tests caught it. Restores every file.

    python scripts/break_it.py            # all mutations
    python scripts/break_it.py fill_once  # only mutations whose name contains this text

Needs the same PostgreSQL as the tests. A mutation that no test catches is a missing test.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    extra: tuple[tuple[str, str], ...] = ()  # more (old, new) edits in the same file


MUTATIONS = [
    # --- version 2 rules ---
    Mutation(
        "money_trigger_removed",
        "migrations/versions/0002_contact_fill_once.py",
        """    op.execute(
        "CREATE TRIGGER trg_donations_money_immutable BEFORE UPDATE ON donations "
        "FOR EACH ROW EXECUTE FUNCTION donations_reject_money_update()"
    )""",
        "    pass",
    ),
    Mutation(
        "money_trigger_ignores_currency",
        "migrations/versions/0002_contact_fill_once.py",
        "       OR NEW.currency IS DISTINCT FROM OLD.currency\n",
        "",
    ),
    Mutation(
        "money_trigger_ignores_donated_at",
        "migrations/versions/0002_contact_fill_once.py",
        "       OR NEW.donated_at IS DISTINCT FROM OLD.donated_at\n",
        "",
    ),
    Mutation(
        "fill_once_condition_removed",
        "donation_ledger/contacts.py",
        ".where(Donation.id == row.id, getattr(Donation, field).is_(None))",
        ".where(Donation.id == row.id)",
    ),
    Mutation(
        "fill_once_overwrites_existing_value",
        "donation_ledger/contacts.py",
        "    if current is None:\n        if _fill(",
        "    if True:\n        if _fill(",
        extra=(
            (
                ".where(Donation.id == row.id, getattr(Donation, field).is_(None))",
                ".where(Donation.id == row.id)",
            ),
        ),
    ),
    Mutation(
        "fill_once_blank_read_not_ignored",
        "donation_ledger/contacts.py",
        "    if new is None:\n        return  # a read without a value never clears anything\n",
        "",
    ),
    Mutation(
        "fill_name_without_is_anonymous",
        "donation_ledger/contacts.py",
        '        values["is_anonymous"] = False\n',
        "        pass\n",
    ),
    Mutation(
        "fill_without_audit_row",
        "donation_ledger/contacts.py",
        "    session.add(\n        DonationContactChange(\n            donation_id=row.id, field_name=field, source=source, sync_run_id=run_id\n        )\n    )\n    session.flush()\n",
        "",
    ),
    Mutation(
        "audit_written_outside_the_batch_transaction",
        "donation_ledger/contacts.py",
        "    session.add(\n        DonationContactChange(\n            donation_id=row.id, field_name=field, source=source, sync_run_id=run_id\n        )\n    )\n    session.flush()\n",
        "    with Session(session.get_bind()) as other, other.begin():\n        other.add(\n            DonationContactChange(\n                donation_id=row.id, field_name=field, source=source, sync_run_id=run_id\n            )\n        )\n",
    ),
    Mutation(
        "differing_contact_value_not_reported",
        "donation_ledger/contacts.py",
        "    if current != new:\n        out.issues +=",
        "    if False:\n        out.issues +=",
    ),
    Mutation(
        "money_difference_not_reported",
        "donation_ledger/contacts.py",
        "        if changed_money:",
        "        if False:",
    ),
    Mutation(
        "quarantine_written_outside_the_batch_transaction",
        "donation_ledger/sync.py",
        "    record_rejection(session, rej, run_id)\n    c.rows_quarantined += 1",
        "    with Session(session.get_bind()) as other, other.begin():\n        record_rejection(other, rej, run_id)\n    c.rows_quarantined += 1",
    ),
    Mutation(
        "invalid_email_rejects_the_donation",
        "donation_ledger/sheet_rows.py",
        "        email = normalize_email(email_cell)\n",
        '        email = normalize_email(email_cell)\n        if email.invalid_text is not None:\n            raise RowError("invalid_email: rejected")\n',
    ),
    Mutation(
        "email_without_domain_dot_accepted",
        "donation_ledger/parsing.py",
        '    if not local or "." not in domain:',
        "    if not local:",
    ),
    Mutation(
        "email_not_lowercased",
        "donation_ledger/parsing.py",
        "    email = text.lower()\n",
        "    email = text\n",
    ),
    Mutation(
        "email_length_limit_removed",
        "donation_ledger/parsing.py",
        "    if len(email) > MAX_EMAIL_LEN or email.count",
        "    if email.count",
    ),
    Mutation(
        "header_does_not_require_donor_email",
        "donation_ledger/sheet_rows.py",
        '    "donor_email": "donor email",\n',
        "",
    ),
    Mutation(
        "invalid_email_warning_not_recorded",
        "donation_ledger/contacts.py",
        "        if item.invalid_email is not None:\n",
        "        if False:\n",
    ),
    Mutation(
        "stripe_reads_billing_details",
        "donation_ledger/stripe_mapping.py",
        '    name = _first_text(metadata.get("donor_name"))',
        '    charge = pi.get("latest_charge")\n    billing = charge.get("billing_details") if isinstance(charge, Mapping) else None\n    name = _first_text(metadata.get("donor_name"), (billing or {}).get("name"))',
    ),
    Mutation(
        "stripe_ignores_metadata_email_fallback",
        "donation_ledger/stripe_mapping.py",
        'return name, _email(raw.get("receipt_email"), raw_meta.get("donor_email"))',
        'return name, _email(raw.get("receipt_email"))',
    ),
    Mutation(
        "stripe_email_validated_after_sanitising",
        "donation_ledger/stripe_mapping.py",
        'return name, _email(raw.get("receipt_email"), raw_meta.get("donor_email"))',
        'return name, _email(pi.get("receipt_email"), metadata.get("donor_email"))',
    ),
    Mutation(
        "email_with_lone_surrogate_accepted",
        "donation_ledger/parsing.py",
        '("Cc", "Cf", "Cs")',
        '("Cc", "Cf")',
    ),
    Mutation(
        "stripe_currency_whitelist_restored",
        "donation_ledger/stripe_mapping.py",
        "    if len(code) != 3 or not code.isascii() or not code.isalpha():",
        "    from donation_ledger.parsing import ISO_4217\n\n    if code.upper() not in ISO_4217 or not code.isascii():",
    ),
    Mutation(
        "stripe_currency_accepts_non_letters",
        "donation_ledger/stripe_mapping.py",
        "    if len(code) != 3 or not code.isascii() or not code.isalpha():",
        "    if len(code) != 3:",
    ),
    Mutation(
        "reconcile_does_not_redrive_old_quarantines",
        "donation_ledger/reconcile.py",
        "        for redo in chunks(_open_stripe_rejections(engine), batch_size):",
        "        for redo in chunks([], batch_size):",
    ),
    Mutation(
        "warning_recorded_outside_the_batch_transaction",
        "donation_ledger/ledger.py",
        "    return 1 if session.execute(stmt).first() is not None else 0",
        "    with Session(session.get_bind()) as other, other.begin():\n        return 1 if other.execute(stmt).first() is not None else 0",
    ),
    Mutation(
        "default_sheet_timezone_changed",
        "donation_ledger/config.py",
        '    sheet_timezone: str = "America/Los_Angeles"',
        '    sheet_timezone: str = "UTC"',
    ),
    Mutation(
        "reconcile_listing_without_per_payment_lock",
        "donation_ledger/reconcile.py",
        "    lock_external_ids(session, SOURCE_STRIPE, [m.external_id for m in mapped_all])",
        "    pass",
    ),
    Mutation(
        "redrive_without_per_payment_lock",
        "donation_ledger/reconcile.py",
        "    lock_external_ids(session, SOURCE_STRIPE, [d.external_id for d in ready])",
        "    pass",
    ),
    Mutation(
        "redrive_keeps_stale_quarantine_beside_existing_donation",
        "donation_ledger/reconcile.py",
        "    resolve_rejections_for(session, SOURCE_STRIPE, held)",
        "    pass",
    ),
    Mutation(
        "email_with_inner_whitespace_accepted",
        "donation_ledger/parsing.py",
        '        ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Cs") for ch in email',
        '        unicodedata.category(ch) in ("Cc", "Cf", "Cs") for ch in email',
    ),
    Mutation(
        "sheet_currency_whitelist_removed",
        "donation_ledger/parsing.py",
        "    if not raw.isascii() or code not in ISO_4217:",
        "    if not raw.isascii() or len(code) != 3:",
    ),
    Mutation(
        "followups_leaks_email",
        "donation_ledger/api.py",
        '    reason: Literal["missing_email", "invalid_email"]\n',
        '    reason: Literal["missing_email", "invalid_email"]\n    email: str = "leak"\n',
    ),
    Mutation(
        "followups_lists_donations_that_have_an_email",
        "donation_ledger/api.py",
        "            .where(Donation.donor_email.is_(None))\n",
        "",
    ),
    Mutation(
        "followups_without_auth",
        "donation_ledger/api.py",
        '@app.get("/followups", response_model=FollowupPage, dependencies=[Depends(require_token)])',
        '@app.get("/followups", response_model=FollowupPage)',
    ),
    Mutation(
        "webhook_duplicate_does_not_fill",
        "donation_ledger/webhook.py",
        "        apply_reads(session, [mapped], inserted, run_id=None)\n",
        "",
    ),
    Mutation(
        "reconcile_does_not_fill",
        "donation_ledger/reconcile.py",
        "    outcome = apply_reads(session, donations, inserted, run_id)\n",
        "    outcome = apply_reads(session, [], inserted, run_id)\n",
    ),
    # --- version 1 properties (SPEC section 3 list) ---
    Mutation(
        "unique_constraint_removed",
        "migrations/versions/0001_initial.py",
        '        sa.UniqueConstraint("source", "external_id", name="uq_donations_source_external_id"),\n',
        "",
    ),
    Mutation(
        "signature_not_verified",
        "donation_ledger/webhook.py",
        "        stripe.Webhook.construct_event(raw_body, signature_header, secret, tolerance=tolerance)\n",
        "",
    ),
    Mutation(
        "amount_parsed_with_float",
        "donation_ledger/parsing.py",
        "    minor = int(value.scaleb(exponent))",
        "    minor = int(float(value) * 10**exponent)",
    ),
    Mutation(
        "sheet_timezone_ignored",
        "donation_ledger/parsing.py",
        "        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))",
        '        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))',
    ),
    Mutation(
        "sync_lock_not_taken",
        "donation_ledger/runs.py",
        "        if not acquired:\n            raise LockNotAcquiredError(source)",
        "        if False:\n            raise LockNotAcquiredError(source)",
    ),
    Mutation(
        "batch_not_atomic_commit_per_statement",
        "donation_ledger/sync.py",
        "    inserted = insert_donations(session, to_insert)\n    c.rows_inserted += len(inserted)\n",
        "    inserted = insert_donations(session, to_insert)\n    session.commit()\n    c.rows_inserted += len(inserted)\n",
    ),
]


def run_tests() -> list[str]:
    """Names of failing tests ([] = the suite passed). The harness's own test is excluded: it
    checks mutation targets against the tree, which a mutation legitimately changes."""
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider",
            "--maxfail=1", "--ignore=tests/test_break_it_script.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )  # fmt: skip
    names = [
        ln.split(" ")[1] for ln in proc.stdout.splitlines() if ln.startswith(("FAILED ", "ERROR "))
    ]
    if proc.returncode != 0 and not names:
        names = ["(pytest exited non-zero)"]
    return names


def stale(mutations: list[Mutation]) -> list[str]:
    """Names of mutations whose target text is not found EXACTLY ONCE (or that change nothing)."""
    bad: list[str] = []
    for m in mutations:
        text = (ROOT / m.path).read_text(encoding="utf-8")
        edits = [(m.old, m.new), *m.extra]
        if any(text.count(old) != 1 for old, _ in edits) or all(old == new for old, new in edits):
            bad.append(f"{m.name} ({m.path})")
    return bad


def main(argv: list[str]) -> int:
    selected = [m for m in MUTATIONS if not argv or any(a in m.name for a in argv)]
    # Check every selected target BEFORE running anything: a stale mutation would otherwise
    # be skipped and the run could still look green.
    broken = stale(selected)
    if broken:
        print("ERROR: these mutations no longer match the code exactly once; update them:")
        for name in broken:
            print(f"  - {name}")
        print("Nothing was run.")
        return 2
    baseline = run_tests()
    if baseline:
        print(
            f"ERROR: the unmutated suite is not green ({baseline[0]}); results would be meaningless."
        )
        return 2
    missed = 0
    for m in selected:
        path = ROOT / m.path
        original = path.read_text(encoding="utf-8")
        try:
            mutated = original.replace(m.old, m.new)
            for old, new in m.extra:
                mutated = mutated.replace(old, new)
            path.write_text(mutated, encoding="utf-8")
            failed = run_tests()
        finally:
            path.write_text(original, encoding="utf-8")
        verdict = "CAUGHT" if failed else "NOT CAUGHT  <-- add a test"
        missed += 0 if failed else 1
        print(f"{verdict}: {m.name}" + (f"  (first failing: {failed[0]})" if failed else ""))
    print(f"\n{len(selected) - missed}/{len(selected)} mutations caught")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
