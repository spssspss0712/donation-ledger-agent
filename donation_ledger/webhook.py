"""Stripe webhook processing: signature check + idempotent write. Framework-free."""

from __future__ import annotations

import enum
import json
import logging
from collections.abc import Mapping
from typing import Any

import stripe
from sqlalchemy.orm import Session, sessionmaker

from donation_ledger.ledger import Rejection, fingerprint_of, insert_donations, record_rejection
from donation_ledger.models import SOURCE_STRIPE
from donation_ledger.stripe_mapping import map_payment_intent, sanitize

log = logging.getLogger(__name__)

HANDLED_EVENT_TYPE = "payment_intent.succeeded"


class SignatureError(Exception):
    """Signature missing/invalid, or the payload is not a valid Stripe event."""


class Outcome(enum.Enum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    QUARANTINED = "quarantined"


def verify_and_parse(
    raw_body: bytes, signature_header: str | None, secret: str, tolerance: int
) -> dict[str, Any]:
    """Verify against the RAW bytes (never a re-serialisation), then parse them."""
    if not signature_header:
        raise SignatureError("missing Stripe-Signature header")
    try:
        stripe.Webhook.construct_event(raw_body, signature_header, secret, tolerance=tolerance)
        parsed = json.loads(raw_body)
    except (stripe.SignatureVerificationError, ValueError) as exc:
        raise SignatureError(type(exc).__name__) from exc
    if not isinstance(parsed, dict):
        raise SignatureError("event is not a JSON object")
    return parsed


def process_event(session_factory: sessionmaker[Session], event: Mapping[str, Any]) -> Outcome:
    """Apply one verified event. Safe to call any number of times, from any thread."""
    event_type = event.get("type")
    if event_type != HANDLED_EVENT_TYPE:
        # Refunds, disputes and everything else are out of scope: log, write nothing.
        log.info(
            "ignored stripe event", extra={"event_id": _s(event.get("id")), "type": _s(event_type)}
        )
        return Outcome.IGNORED
    data = event.get("data")
    obj = data.get("object") if isinstance(data, Mapping) else None
    mapped: Any
    if isinstance(obj, Mapping):
        mapped = map_payment_intent(obj)
    else:
        safe = sanitize(dict(event))
        mapped = Rejection(
            source=SOURCE_STRIPE,
            external_id=None,
            raw_row=safe,
            reason="invalid_event: data.object missing",
            fingerprint=fingerprint_of(safe),
        )
    with session_factory() as session, session.begin():
        if isinstance(mapped, Rejection):
            record_rejection(session, mapped, run_id=None)
            log.warning(
                "stripe event quarantined",
                extra={"event_id": _s(event.get("id")), "reason": mapped.reason},
            )
            return Outcome.QUARANTINED
        inserted = insert_donations(session, [mapped])
    outcome = Outcome.CREATED if inserted else Outcome.DUPLICATE
    log.info(
        "stripe payment processed",
        extra={
            "event_id": _s(event.get("id")),
            "external_id": mapped.external_id,
            "outcome": outcome.value,
        },
    )
    return outcome


def _s(value: Any) -> str | None:
    return value[:80] if isinstance(value, str) else None
