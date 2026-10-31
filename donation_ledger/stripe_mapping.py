"""Pure mapping of a Stripe PaymentIntent object to a ledger row (or a rejection).

Field choices follow the Stripe API reference for the PaymentIntent object:
``id`` (pi_...), ``amount`` / ``amount_received`` (smallest unit), ``currency`` (lowercase),
``created`` (Unix seconds). A PaymentIntent has no donor name; it is read, when present, from
the (optionally expanded) latest charge's ``billing_details`` or from ``metadata``.
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
    RowError,
    clean_stripe_text,
    is_visually_empty,
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
        donor_email=email,
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


def _donor(pi: Mapping[str, Any]) -> tuple[str | None, str | None]:
    charge = _first_charge(pi)
    billing = charge.get("billing_details") if charge else None
    billing = billing if isinstance(billing, Mapping) else {}
    metadata = pi.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    shipping = pi.get("shipping")
    shipping = shipping if isinstance(shipping, Mapping) else {}
    name = _first_text(
        billing.get("name"), metadata.get("donor_name"), metadata.get("name"), shipping.get("name")
    )
    email = _first_text(
        billing.get("email"),
        pi.get("receipt_email"),
        metadata.get("donor_email"),
        metadata.get("email"),
    )
    if name is not None and (name.casefold() == "anonymous" or len(name) > MAX_NAME_LEN):
        name = None if name.casefold() == "anonymous" else name[:MAX_NAME_LEN]
    if email is not None:
        email = email.lower() if "@" in email and len(email) <= 320 else None
    return name, email


def _first_charge(pi: Mapping[str, Any]) -> Mapping[str, Any] | None:
    latest = pi.get("latest_charge")
    if isinstance(latest, Mapping):
        return latest
    charges = pi.get("charges")  # legacy API versions
    if isinstance(charges, Mapping):
        data = charges.get("data")
        if isinstance(data, list) and data and isinstance(data[0], Mapping):
            return data[0]
    return None


def _first_text(*candidates: Any) -> str | None:
    for candidate in candidates:
        if isinstance(candidate, str):
            text = " ".join(strip_invisible(candidate).split())
            if text and not is_visually_empty(text):
                return text
    return None
