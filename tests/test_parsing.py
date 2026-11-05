from datetime import UTC, datetime

import pytest

from donation_ledger.parsing import (
    RowError,
    clean_stripe_text,
    minor_units_from_decimal_string,
    normalize_currency,
    normalize_donor_name,
    parse_sheet_datetime,
)


@pytest.mark.parametrize(
    ("text", "currency", "expected"),
    [
        ("1000", "USD", 100000),
        ("1,000.50", "USD", 100050),
        ("$1,000.50", "USD", 100050),
        ("  $ 12.5 ", "USD", 1250),
        ("0.01", "USD", 1),
        ("19.99", "USD", 1999),
        ("1000", "JPY", 1000),  # zero-decimal currency
        ("1.234", "KWD", 1234),  # three-decimal currency
        ("1,234,567.89", "USD", 123456789),
    ],
)
def test_amount_formats_parse_to_exact_integer_minor_units(
    text: str, currency: str, expected: int
) -> None:
    assert minor_units_from_decimal_string(text, currency) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "abc",
        "-5",
        "(5.00)",
        "0",
        "0.00",
        "$0",
        "1.005",
        "1,00",
        "1.000,50",
        "1e3",
        "NaN",
        "Infinity",
        "€5",
        "5 USD",
        "$$5",
        "1,2,3",
        "12.",
        ".5",
        "١٢٣",
        "99999999999999999999",
    ],
)
def test_unparseable_zero_negative_or_ambiguous_amounts_are_rejected(text: str) -> None:
    with pytest.raises(RowError):
        minor_units_from_decimal_string(text, "USD")


def test_fractional_amount_is_rejected_for_zero_decimal_currency() -> None:
    with pytest.raises(RowError):
        minor_units_from_decimal_string("10.50", "JPY")


def test_amount_parsing_never_goes_through_floating_point() -> None:
    # 0.1 + 0.2 style traps: 4.35 * 100 == 434.99999999999994 with floats.
    assert minor_units_from_decimal_string("4.35", "USD") == 435
    assert minor_units_from_decimal_string("1.15", "USD") == 115


def test_blank_currency_defaults_to_usd_and_codes_are_uppercased() -> None:
    assert normalize_currency("") == "USD"
    assert normalize_currency("  eur ") == "EUR"


@pytest.mark.parametrize("code", ["US", "DOLLAR", "U$D", "XXX1", "ZZZ"])
def test_invalid_currency_codes_are_rejected(code: str) -> None:
    with pytest.raises(RowError):
        normalize_currency(code)


@pytest.mark.parametrize("name", ["", "   ", "anonymous", "ANONYMOUS", " Anonymous "])
def test_blank_or_anonymous_names_become_none(name: str) -> None:
    assert normalize_donor_name(name) is None


def test_real_names_are_whitespace_normalised_and_kept() -> None:
    assert normalize_donor_name("  Ada   Lovelace ") == "Ada Lovelace"
    assert normalize_donor_name("Anonymous Smith") == "Anonymous Smith"


def test_names_with_nul_characters_are_rejected() -> None:
    with pytest.raises(RowError):
        normalize_donor_name("Ada\x00")


def test_overlong_names_are_rejected() -> None:
    with pytest.raises(RowError):
        normalize_donor_name("x" * 501)


def test_sheet_date_is_interpreted_in_the_configured_timezone() -> None:
    # 2024-01-15 is PST (UTC-8): midnight local is 08:00 UTC.
    assert parse_sheet_datetime("2024-01-15", "America/Los_Angeles") == datetime(
        2024, 1, 15, 8, 0, tzinfo=UTC
    )
    # 2024-07-15 is PDT (UTC-7).
    assert parse_sheet_datetime("7/15/2024 13:30", "America/Los_Angeles") == datetime(
        2024, 7, 15, 20, 30, tzinfo=UTC
    )


def test_late_evening_local_time_lands_on_the_next_utc_day() -> None:
    assert parse_sheet_datetime("2024-12-31 23:30", "America/Los_Angeles") == datetime(
        2025, 1, 1, 7, 30, tzinfo=UTC
    )


def test_explicit_offsets_in_the_cell_override_the_configured_timezone() -> None:
    assert parse_sheet_datetime("2024-03-05T10:00:00+00:00", "Asia/Tokyo") == datetime(
        2024, 3, 5, 10, 0, tzinfo=UTC
    )


def test_ambiguous_dst_fallback_time_resolves_to_first_occurrence() -> None:
    # 2024-11-03 01:30 happens twice in Los Angeles; the first one is PDT (UTC-7).
    assert parse_sheet_datetime("2024-11-03 01:30", "America/Los_Angeles") == datetime(
        2024, 11, 3, 8, 30, tzinfo=UTC
    )


def test_nonexistent_spring_forward_time_resolves_deterministically_to_the_shifted_instant() -> (
    None
):
    # 2026-03-08 02:30 does not exist in Los Angeles (clocks jump 02:00 -> 03:00 PDT, UTC-7);
    # zoneinfo reads it with the pre-transition offset (UTC-8), i.e. 10:30Z = 03:30 PDT.
    assert parse_sheet_datetime("3/8/2026 02:30", "America/Los_Angeles") == datetime(
        2026, 3, 8, 10, 30, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "text", ["", "not a date", "13/45/2024", "2024-02-30", "1999-12-31", "2999-01-01", "45000"]
)
def test_invalid_or_implausible_dates_are_rejected(text: str) -> None:
    with pytest.raises(RowError):
        parse_sheet_datetime(text, "America/Los_Angeles")


def test_stripe_text_cleaning_removes_nul_and_lone_surrogates() -> None:
    assert clean_stripe_text("a\x00b\ud800c") == "ab?c"


def test_ugx_is_a_two_decimal_currency_like_stripe_treats_it() -> None:
    # Stripe's API expresses UGX amounts x100, so sheet and Stripe rows must agree.
    assert minor_units_from_decimal_string("5000", "UGX") == 500000


# --- donor email (SPEC 2.3 "Donor email") ---------------------------------------------------

from donation_ledger.parsing import EmailResult, normalize_email  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Ann@Example.ORG  ", "ann@example.org"),
        ("a@b.co", "a@b.co"),
        ("first.last+tag@sub.example.org", "first.last+tag@sub.example.org"),
        ("\tx@y.org\n", "x@y.org"),
        ("a" * 248 + "@b.org", "a" * 248 + "@b.org"),  # exactly 254 characters
    ],
)
def test_valid_emails_are_trimmed_and_lowercased(raw: str, expected: str) -> None:
    assert normalize_email(raw) == EmailResult(expected, None)


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", "​", None])
def test_blank_email_is_missing_not_invalid(raw: str | None) -> None:
    assert normalize_email(raw) == EmailResult(None, None)


@pytest.mark.parametrize(
    "raw",
    [
        "not-an-email",
        "two@@signs.org",
        "a@b@c.org",
        "no space@example.org",
        "a@exa mple.org",
        "a@nodot",
        "@example.org",
        "user@",
        "user@.org",
        "user@example.",
        "user@exa..mple.org",
        "a" * 249 + "@b.org",  # 255 characters
        "a@b.org\x00",
        "a b@example.org",
        "mailto:a@b.org c",
    ],
)
def test_malformed_email_becomes_none_and_is_flagged_invalid(raw: str) -> None:
    result = normalize_email(raw)
    assert result.email is None
    assert result.invalid_text == raw


def test_non_string_email_value_is_flagged_invalid_not_a_crash() -> None:
    assert normalize_email(5) == EmailResult(None, "5")


@pytest.mark.parametrize("raw", ["a\ud800@b.org", "a\udfff@b.org", "\ud800@b.org"])
def test_lone_surrogates_in_an_email_make_it_invalid_instead_of_poisoning_the_batch(
    raw: str,
) -> None:
    assert normalize_email(raw).email is None
