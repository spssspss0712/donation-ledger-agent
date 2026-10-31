"""Pure parsing and normalisation helpers. No I/O, no database."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

MAX_NAME_LEN = 500
MAX_ADDRESS_LEN = 2000
MAX_ID_LEN = 200
# Far below BIGINT; larger values are almost certainly typos.
MAX_AMOUNT_MINOR = 10**15

# ISO 4217 minor-unit exponents that differ from the default of 2 (Stripe's list).
ZERO_DECIMAL = frozenset(
    [
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "JPY",
        "KMF",
        "KRW",
        "MGA",
        "PYG",
        "RWF",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    ]
)
THREE_DECIMAL = frozenset(["BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"])

# Active ISO 4217 alphabetic codes. Unknown codes are quarantined, not guessed.
ISO_4217 = frozenset(
    [
        "AED",
        "AFN",
        "ALL",
        "AMD",
        "ANG",
        "AOA",
        "ARS",
        "AUD",
        "AWG",
        "AZN",
        "BAM",
        "BBD",
        "BDT",
        "BGN",
        "BHD",
        "BIF",
        "BMD",
        "BND",
        "BOB",
        "BRL",
        "BSD",
        "BTN",
        "BWP",
        "BYN",
        "BZD",
        "CAD",
        "CDF",
        "CHF",
        "CLP",
        "CNY",
        "COP",
        "CRC",
        "CUP",
        "CVE",
        "CZK",
        "DJF",
        "DKK",
        "DOP",
        "DZD",
        "EGP",
        "ERN",
        "ETB",
        "EUR",
        "FJD",
        "FKP",
        "GBP",
        "GEL",
        "GHS",
        "GIP",
        "GMD",
        "GNF",
        "GTQ",
        "GYD",
        "HKD",
        "HNL",
        "HTG",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "IQD",
        "IRR",
        "ISK",
        "JMD",
        "JOD",
        "JPY",
        "KES",
        "KGS",
        "KHR",
        "KMF",
        "KPW",
        "KRW",
        "KWD",
        "KYD",
        "KZT",
        "LAK",
        "LBP",
        "LKR",
        "LRD",
        "LSL",
        "LYD",
        "MAD",
        "MDL",
        "MGA",
        "MKD",
        "MMK",
        "MNT",
        "MOP",
        "MRU",
        "MUR",
        "MVR",
        "MWK",
        "MXN",
        "MYR",
        "MZN",
        "NAD",
        "NGN",
        "NIO",
        "NOK",
        "NPR",
        "NZD",
        "OMR",
        "PAB",
        "PEN",
        "PGK",
        "PHP",
        "PKR",
        "PLN",
        "PYG",
        "QAR",
        "RON",
        "RSD",
        "RUB",
        "RWF",
        "SAR",
        "SBD",
        "SCR",
        "SDG",
        "SEK",
        "SGD",
        "SHP",
        "SLE",
        "SOS",
        "SRD",
        "SSP",
        "STN",
        "SVC",
        "SYP",
        "SZL",
        "THB",
        "TJS",
        "TMT",
        "TND",
        "TOP",
        "TRY",
        "TTD",
        "TWD",
        "TZS",
        "UAH",
        "UGX",
        "USD",
        "UYU",
        "UZS",
        "VES",
        "VND",
        "VUV",
        "WST",
        "XAF",
        "XCD",
        "XOF",
        "XPF",
        "YER",
        "ZAR",
        "ZMW",
        "ZWL",
    ]
)

_AMOUNT_RE = re.compile(r"^\$?\s*(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d+))?$", re.ASCII)

# Earliest plausible donation and how far in the future we tolerate (clock/timezone slack).
MIN_DATE = datetime(2000, 1, 1, tzinfo=UTC)
FUTURE_SLACK = timedelta(days=2)

_US_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %I:%M:%S %p",
)


class RowError(ValueError):
    """A single input row/field is unacceptable. The message is a PII-free reason."""


def has_nul(text: str) -> bool:
    return "\x00" in text


def exponent_for(currency: str) -> int:
    if currency in ZERO_DECIMAL:
        return 0
    if currency in THREE_DECIMAL:
        return 3
    return 2


def minor_units_from_decimal_string(text: str, currency: str) -> int:
    """Parse "1,000.50" / "$1000" into smallest-unit integers using Decimal only."""
    stripped = text.strip()
    match = _AMOUNT_RE.match(stripped)
    if match is None:
        raise RowError("invalid_amount: not a plain positive number")
    exponent = exponent_for(currency)
    frac = match.group("frac") or ""
    if len(frac) > exponent:
        raise RowError(f"invalid_amount: more than {exponent} decimal places for {currency}")
    try:
        value = Decimal(match.group("int").replace(",", "") + ("." + frac if frac else ""))
    except InvalidOperation as exc:  # pragma: no cover - regex already guarantees digits
        raise RowError("invalid_amount: not a number") from exc
    minor = int(value.scaleb(exponent))
    if minor <= 0:
        raise RowError("invalid_amount: must be greater than zero")
    if minor > MAX_AMOUNT_MINOR:
        raise RowError("invalid_amount: implausibly large")
    return minor


def normalize_currency(text: str) -> str:
    code = text.strip().upper()
    if code == "":
        return "USD"
    if code not in ISO_4217:
        raise RowError("invalid_currency: not a known ISO 4217 code")
    return code


def normalize_donor_name(text: str) -> str | None:
    """Return None for anonymous donors; "Anonymous" is never stored."""
    if has_nul(text):
        raise RowError("invalid_text: contains NUL character")
    name = " ".join(text.split())
    if name == "" or name.casefold() == "anonymous":
        return None
    if len(name) > MAX_NAME_LEN:
        raise RowError("invalid_name: too long")
    return name


def parse_sheet_datetime(text: str, tz_name: str, now: datetime | None = None) -> datetime:
    """Interpret a sheet date in ``tz_name`` (unless the cell carries an offset) -> UTC."""
    value = text.strip()
    if value == "":
        raise RowError("invalid_date: empty")
    parsed = _parse_naive_or_aware(value)
    if parsed is None:
        raise RowError("invalid_date: unrecognised format")
    if parsed.tzinfo is None:
        # fold=0 picks the first occurrence of an ambiguous (DST fall-back) wall time.
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    try:
        utc = parsed.astimezone(UTC)
    except (OverflowError, ValueError) as exc:  # e.g. 9999-12-31 23:00 shifted past year 9999
        raise RowError("invalid_date: outside plausible range") from exc
    current = now or datetime.now(UTC)
    if utc < MIN_DATE or utc > current + FUTURE_SLACK:
        raise RowError("invalid_date: outside plausible range")
    return utc


def _parse_naive_or_aware(value: str) -> datetime | None:
    if not value[:1].isdigit():
        return None
    try:
        return datetime.fromisoformat(value) if "-" in value else _parse_us(value)
    except ValueError:
        return None


def _parse_us(value: str) -> datetime | None:
    for fmt in _US_FORMATS:
        try:
            return datetime.strptime(value, fmt)  # noqa: DTZ007 - tz applied by the caller
        except ValueError:
            continue
    return None


def clean_stripe_text(text: str) -> str:
    """Make a string safe for PostgreSQL: no NUL, no lone surrogates."""
    return text.replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")
