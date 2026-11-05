"""Pure parsing and normalisation helpers. No I/O, no database."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
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

# ISO 4217 alphabetic codes. Unknown codes are quarantined, not guessed. Besides the active
# codes it keeps recently retired/introduced ones (SLL, STD, MRO, BYR, XCG, ZWG) because
# Stripe can still report them and a real payment must never be dropped for its currency.
ISO_4217 = frozenset(
    [
        "SLL",
        "STD",
        "MRO",
        "BYR",
        "XCG",
        "ZWG",
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
# "$" is only accepted for currencies that are dollars/pesos written with that sign.
DOLLAR_FAMILY = frozenset(
    [
        "USD",
        "CAD",
        "AUD",
        "NZD",
        "HKD",
        "SGD",
        "TWD",
        "MXN",
        "ARS",
        "CLP",
        "COP",
        "BSD",
        "BBD",
        "BMD",
        "BZD",
        "BND",
        "FJD",
        "GYD",
        "JMD",
        "KYD",
        "LRD",
        "NAD",
        "SBD",
        "SRD",
        "TTD",
        "XCD",
    ]
)

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


def is_visually_empty(text: str) -> bool:
    """True for blank text or text made only of whitespace/format chars (e.g. U+200B)."""
    return all(ch.isspace() or unicodedata.category(ch) == "Cf" for ch in text)


def strip_invisible(text: str) -> str:
    """Strip whitespace and invisible format chars (ZWSP, ...) from both ends."""
    start, end = 0, len(text)
    while start < end and (text[start].isspace() or unicodedata.category(text[start]) == "Cf"):
        start += 1
    while end > start and (text[end - 1].isspace() or unicodedata.category(text[end - 1]) == "Cf"):
        end -= 1
    return text[start:end]


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
    if stripped.startswith("$") and currency not in DOLLAR_FAMILY:
        raise RowError("invalid_amount: $ sign contradicts the currency column")
    exponent = exponent_for(currency)
    frac = match.group("frac") or ""
    if len(match.group("int")) > 25:  # before Decimal/int: huge digit strings are quadratic
        raise RowError("invalid_amount: implausibly large")
    if len(frac) > exponent:
        # Only "5000.00" for a zero-decimal currency is tolerated. "1.000" in a 2-decimal
        # currency may be a European thousands separator (1,000), so it must be quarantined.
        if exponent != 0 or frac.strip("0") or len(frac) > 2:
            raise RowError(f"invalid_amount: more than {exponent} decimal places for {currency}")
        frac = frac[:exponent]
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
    raw = strip_invisible(text)
    if raw == "":
        return "USD"
    # Check ASCII before upper(): "\u0131nr".upper() == "INR" would "fix" garbage silently.
    code = raw.upper()
    if not raw.isascii() or code not in ISO_4217:
        raise RowError("invalid_currency: not a known ISO 4217 code")
    return code


def normalize_donor_name(text: str) -> str | None:
    """Return None for anonymous donors; "Anonymous" is never stored."""
    if has_nul(text):
        raise RowError("invalid_text: contains NUL character")
    name = " ".join(strip_invisible(text).split())
    if is_visually_empty(name) or name.casefold() == "anonymous":
        return None
    if len(name) > MAX_NAME_LEN:
        raise RowError("invalid_name: too long")
    return name


MAX_EMAIL_LEN = 254  # RFC 5321 path limit


@dataclass(frozen=True)
class EmailResult:
    """``email`` is the normalised address or None; ``invalid_text`` is set only when a
    non-blank value was given but rejected (so the caller can record a warning)."""

    email: str | None
    invalid_text: str | None


def normalize_email(value: object) -> EmailResult:
    """Trim, lowercase, and apply the deliberately simple syntax check from SPEC 2.3.

    Never raises: contact information must not stop a donation from being recorded.
    """
    if value is None:
        return EmailResult(None, None)
    if not isinstance(value, str):
        return EmailResult(None, str(value)[: MAX_EMAIL_LEN * 2])
    text = value.strip()
    if is_visually_empty(text):
        return EmailResult(None, None)
    email = text.lower()
    if _email_is_valid(email):
        return EmailResult(email, None)
    return EmailResult(None, value)


def _email_is_valid(email: str) -> bool:
    if len(email) > MAX_EMAIL_LEN or email.count("@") != 1:
        return False
    # U+FFFD marks bytes that were not valid UTF-8: storing it would be silent corruption.
    if "\ufffd" in email or any(
        ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Cs") for ch in email
    ):
        return False
    local, domain = email.split("@")
    if not local or "." not in domain:
        return False
    return not (domain.startswith(".") or domain.endswith(".") or ".." in domain)


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
