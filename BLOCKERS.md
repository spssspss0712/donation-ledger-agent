# BLOCKERS and ASSUMPTIONS

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
