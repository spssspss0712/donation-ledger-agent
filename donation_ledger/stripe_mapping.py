"""Pure mapping of a Stripe PaymentIntent object to a ledger row (or a rejection).

Field choices follow the Stripe API reference for the PaymentIntent object:
``id`` (pi_...), ``amount`` / ``amount_received`` (smallest unit), ``currency`` (lowercase),
``created`` (Unix seconds). A PaymentIntent has no top-level donor name and no ``charges``
list: the donor name comes from ``metadata.donor_name`` and the email from ``receipt_email``
or ``metadata.donor_email`` (both controlled by our own website).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from donation_ledger.ledger import NewDonation, Rejection, fingerprint_of
from donation_ledger.models import SOURCE_STRIPE
from donation_ledger.parsing import (
    FUTURE_SLACK,
    ISO_4217,
    MAX_AMOUNT_MINOR,
    MAX_NAME_LEN,
    MIN_DATE,
    EmailResult,
    RowError,
    clean_stripe_text,
    is_visually_empty,
    normalize_email,
    strip_invisible,
)


def sanitize(value: Any) -> Any:
    """Recursively make JSON data storable in PostgreSQL JSONB/TEXT."""
    if isinstance(value, str):
        text = clean_stripe_text(value)
        # "Anonymous" is never stored, not even inside raw payloads.
        return "" if strip_invisible(text).casefold() == "anonymous" else text
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # JSONB rejects NaN/Infinity tokens
    if isinstance(value, Mapping):
        return {
            clean_stripe_text(str(k)): sanitize(v)
            for k, v in value.items()
            # Stripe says a PaymentIntent client_secret must not be stored or logged.
            if "client_secret" not in str(k).lower()
        }
    if isinstance(value, list | tuple):
        return [sanitize(v) for v in value]
    return value


def map_payment_intent(pi: Mapping[str, Any]) -> NewDonation | Rejection:
    safe = sanitize(dict(pi))
    external_id = safe.get("id")
    if "\x00" in str(pi.get("id")):
        external_id = None  # stripping NUL must not alias a different real payment id
    try:
        if (
            not isinstance(external_id, str)
            or not external_id.startswith("pi_")
            or len(external_id) < 4
        ):
            raise RowError("invalid_payment_intent: id missing or not a pi_ id")
        if len(external_id) > 255:
            raise RowError("invalid_payment_intent: id too long")
        currency = _currency(safe.get("currency"))
        amount = _amount(safe)
        donated_at = _created(safe.get("created"))
        name, email = _donor(safe)
    except RowError as exc:
        ext = external_id if isinstance(external_id, str) and len(external_id) <= 255 else None
        return Rejection(
            source=SOURCE_STRIPE,
            external_id=ext,
            raw_row=safe,
            reason=str(exc),
            fingerprint=fingerprint_of(safe),
        )
    return NewDonation(
        source=SOURCE_STRIPE,
        external_id=external_id,
        donor_name=name,
        donor_email=email.email,
        invalid_email=email.invalid_text,
        donor_address=None,
        recipient=None,
        amount_minor=amount,
        currency=currency,
        donated_at=donated_at,
        raw_payload=safe,
    )


def _currency(value: Any) -> str:
    code = value.strip() if isinstance(value, str) else ""
    # isascii: str.isalpha() accepts letters like "ǅ" that the DB CHECK would then reject.
    if code.upper() not in ISO_4217 or not code.isascii():
        raise RowError("invalid_currency: missing or not a 3-letter code")
    return code.upper()


def _positive_int(value: Any) -> int | None:
    # bool is an int subclass; True must not be read as 1 cent.
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _amount(pi: Mapping[str, Any]) -> int:
    # amount_received is what was actually captured; it can be lower than amount.
    received = pi.get("amount_received")
    if isinstance(received, int) and not isinstance(received, bool):
        amount = _positive_int(received)  # a succeeded payment that captured 0 is suspect
    else:
        amount = _positive_int(pi.get("amount"))
    if amount is None:
        raise RowError("invalid_amount: missing, zero or negative")
    if amount > MAX_AMOUNT_MINOR:
        raise RowError("invalid_amount: implausibly large")
    return amount


def _created(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RowError("invalid_created: missing or not a Unix timestamp")
    try:
        moment = datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise RowError("invalid_created: out of range") from exc
    if moment < MIN_DATE or moment > datetime.now(UTC) + FUTURE_SLACK:
        raise RowError("invalid_created: outside plausible range")
    return moment


def _donor(pi: Mapping[str, Any]) -> tuple[str | None, EmailResult]:
    """Donor details exactly where the website puts them (SPEC 2.3).

    A PaymentIntent has no top-level name and no ``charges`` list, and the Charge's billing
    details are normally not in the event, so they are deliberately not read.
    """
    metadata = pi.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    name = _first_text(metadata.get("donor_name"))
    if name is not None:
        name = None if name.casefold() == "anonymous" else name[:MAX_NAME_LEN]
    return name, _email(pi.get("receipt_email"), metadata.get("donor_email"))


def _email(*candidates: Any) -> EmailResult:
    """First valid candidate wins; if none is valid, report the first malformed one."""
    first_invalid: EmailResult | None = None
    for candidate in candidates:
        result = normalize_email(candidate)
        if result.email is not None:
            return result
        if result.invalid_text is not None and first_invalid is None:
            first_invalid = result
    return first_invalid or EmailResult(None, None)


def _first_text(*candidates: Any) -> str | None:
    for candidate in candidates:
        if isinstance(candidate, str):
            text = " ".join(strip_invisible(candidate).split())
            if text and not is_visually_empty(text):
                return text
    return None
