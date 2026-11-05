"""Where Stripe donor details come from (SPEC 2.3 "Stripe donor details")."""

from __future__ import annotations

from typing import Any

import pytest

from donation_ledger.ledger import NewDonation
from donation_ledger.stripe_mapping import map_payment_intent
from tests.helpers import payment_intent


def mapped(**extra: Any) -> NewDonation:
    result = map_payment_intent(payment_intent(**extra))
    assert isinstance(result, NewDonation)
    return result


def test_email_is_read_from_receipt_email_and_normalised() -> None:
    d = mapped(receipt_email="  Ada@Example.ORG ")
    assert d.donor_email == "ada@example.org"
    assert d.invalid_email is None


def test_email_falls_back_to_metadata_donor_email_when_receipt_email_is_absent() -> None:
    assert mapped(metadata={"donor_email": "M@Example.org"}).donor_email == "m@example.org"
    assert mapped(receipt_email=None, metadata={"donor_email": "m@x.org"}).donor_email == "m@x.org"


def test_receipt_email_wins_over_metadata_donor_email() -> None:
    d = mapped(receipt_email="first@x.org", metadata={"donor_email": "second@x.org"})
    assert d.donor_email == "first@x.org"


def test_a_valid_metadata_email_is_used_when_receipt_email_is_malformed() -> None:
    d = mapped(receipt_email="garbage", metadata={"donor_email": "ok@x.org"})
    assert d.donor_email == "ok@x.org"


def test_malformed_email_is_stored_as_none_and_flagged_with_the_original_text() -> None:
    d = mapped(receipt_email="not an email")
    assert d.donor_email is None
    assert d.invalid_email == "not an email"
    assert d.raw_payload["receipt_email"] == "not an email"  # original stays in raw_payload


def test_no_email_anywhere_is_missing_not_invalid() -> None:
    d = mapped()
    assert (d.donor_email, d.invalid_email) == (None, None)


def test_name_is_read_from_metadata_donor_name_and_whitespace_is_collapsed() -> None:
    d = mapped(metadata={"donor_name": "  Grace   Hopper "})
    assert d.donor_name == "Grace Hopper"


@pytest.mark.parametrize("name", ["", "   ", "Anonymous", "ANONYMOUS", None, 5])
def test_blank_or_anonymous_or_non_text_name_means_no_name(name: object) -> None:
    assert mapped(metadata={"donor_name": name}).donor_name is None


def test_charge_billing_details_are_out_of_scope_and_ignored() -> None:
    d = mapped(latest_charge={"billing_details": {"name": "Ignored", "email": "ignored@x.org"}})
    assert (d.donor_name, d.donor_email) == (None, None)


def test_legacy_charges_list_shipping_name_and_other_metadata_keys_are_ignored() -> None:
    d = mapped(
        charges={"data": [{"billing_details": {"name": "Old", "email": "old@x.org"}}]},
        shipping={"name": "Ship Name"},
        metadata={"name": "Plain Name", "email": "plain@x.org"},
    )
    assert (d.donor_name, d.donor_email) == (None, None)


def test_stripe_donations_never_carry_an_address_or_recipient() -> None:
    d = mapped(receipt_email="a@b.org", metadata={"donor_name": "A"})
    assert (d.donor_address, d.recipient) == (None, None)


@pytest.mark.parametrize(
    "code", ["xcg", "std", "zwg", "XCG", "ANG", "xyz", "ZZZ", "qqq", " eur ", "hrk", "cuc", "AbC"]
)
def test_any_three_letter_stripe_currency_is_stored_uppercase_even_if_not_an_iso_code(
    code: str,
) -> None:
    from donation_ledger.parsing import ISO_4217

    assert mapped(currency=code).currency == code.strip().upper()
    assert "XYZ" not in ISO_4217 and "QQQ" not in ISO_4217  # the examples really are unlisted


@pytest.mark.parametrize(
    "code", ["us", "usdd", "u$d", "u1d", "", "  ", None, 5, "\u01c5xy", "\u0131nr"]
)
def test_a_stripe_currency_that_cannot_be_a_three_letter_ascii_code_is_still_quarantined(
    code: Any,
) -> None:
    from donation_ledger.ledger import Rejection

    result = map_payment_intent(payment_intent(currency=code))
    assert isinstance(result, Rejection) and result.reason.startswith("invalid_currency")


def test_the_iso_whitelist_still_applies_to_sheet_currencies_where_typos_are_plausible() -> None:
    from donation_ledger.parsing import RowError, normalize_currency

    assert normalize_currency("xcg") == "XCG"
    for typo in ("XYZ", "UDS", "EUROS"):
        with pytest.raises(RowError):
            normalize_currency(typo)


@pytest.mark.parametrize("bad", ["a\x00@b.com", "a\ud800@b.com", "a​@b.com"])
def test_stripe_email_with_nul_or_surrogate_is_invalid_not_silently_rewritten_into_another_address(
    bad: str,
) -> None:
    d = mapped(receipt_email=bad)
    assert d.donor_email is None
    assert d.invalid_email == bad
