# BLOCKERS and ASSUMPTIONS

## Version 2 (branch agent/v2)
- **Blockers:** none for Phase A. Phase B (real Stripe test mode / real Google Sheet) and Phase C (Render) were again not attempted. No `sk_live_` key was seen (only environment variable names were checked, never printed).
- The "known data-quality gap" below (named Stripe donors stored as anonymous because the ledger never back-fills) is **resolved by the v2 specification**: contact fields are filled in once by later reads (duplicate webhook, reconciliation, sheet sync).

### V2 assumptions (safest choice taken; please confirm)
1. **NUL / undecodable bytes vs. "contact info never quarantines".** The spec contradicts itself slightly: Strictness says NUL and undecodable bytes are quarantined; the email rule says a donation is never quarantined because of contact information. Taken: only the *Donor Email* cell is exempt (bad bytes there -> `invalid_email`, row ingested); NUL / bad bytes in any other cell (name, address, recipient too) still quarantine the row. If you want name/address/recipient exempt as well, the row would have to be stored with the bad characters stripped or the field NULL; that changes what the ledger holds for a donor, so it was not done unasked.
2. **Over-long name / address / recipient** (more than 500 / 2000 / 500 characters) are cut and flagged (`contact_truncated`) rather than quarantined (v1 quarantined them), because contact info must never block a donation. Alternative: store NULL and warn.
3. **Money mismatch does not block contact fill** (changed after review): a later read with a different amount/currency/date records `changed_after_sync` and still fills empty contact fields, because the spec treats the same Entry ID as the same donation. Risk: a reused Entry ID could attach another donor's details to an empty row; warnings and the audit table make it visible.
4. **`invalid_email` is also recorded when a donation already has a valid email** and a later read has a malformed one (de-duplicated); harmless, the follow-up list only reads donations whose email is NULL.
5. **Follow-up `date`** is the donation date (`donated_at`), not the date the problem was noticed.
6. **Contact fields are not protected by a trigger** (only money is, as specified), so a human can correct an email with a plain UPDATE.
7. **Migration upgrade test needs `CREATEDB`** on the test role (it creates a scratch database).

## Blockers (nothing was blocked for Phase A)
- None. Phase B (real Stripe test mode, real Google Sheet) and Phase C (Render) were not attempted: no credentials were provided. No `sk_live_` key was found in the environment (checked env var names for `stripe`/`sk_`).

## Known data-quality gap (needs a human decision)
- Real `payment_intent.succeeded` webhooks carry `latest_charge` as an ID string, so billing name/e-mail are normally absent from the event. A named donor can therefore be stored anonymous if the webhook wins the race against reconciliation, because the ledger is append-only and never back-fills. Options: put donor name/e-mail in PaymentIntent metadata on the website (works today); allow reconcile to enrich NULL name/e-mail once (relaxes append-only, needs a decision); or fetch the charge during the webhook (adds a Stripe API call). Only the metadata route is implemented.

## Assumptions (safest choice taken, please confirm)
1. **Stripe donor name/e-mail location.** The spec says name/e-mail "may be present" but a PaymentIntent has neither as top-level fields. Assumed they come from billing details of the expanded latest charge, `receipt_email`, or `metadata.donor_name` / `metadata.donor_email`. If the website stores them elsewhere, update `stripe_mapping._donor`.
2. **Stripe amount** = `amount_received` when positive, else `amount`.
3. **Read-only API auth.** Spec lists no auth. Assumed donor data must not be public: bearer token required, e-mail/address never exposed.
4. **Duplicate `Entry ID` where one copy is already in the ledger.** Both rows are quarantined; the ledger row is left alone.
5. **Rows with an `Entry ID` already in the ledger are never quarantined for field errors**, only warned about (append-only rule).
6. **Date plausibility window** (2000-01-01 .. now + 2 days) is mine; spec silent.
7. **Webhook signed-but-unusable payloads** are quarantined with 200 rather than rejected with 4xx.
8. The Google sheet's first row is the header and the first worksheet is used unless `GOOGLE_WORKSHEET` is set.
