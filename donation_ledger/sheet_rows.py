"""Turn raw sheet cells into validated candidates or quarantine decisions. Pure logic."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from donation_ledger.ledger import NewDonation, Rejection, fingerprint_of
from donation_ledger.models import SOURCE_SHEET
from donation_ledger.parsing import (
    MAX_ADDRESS_LEN,
    MAX_ID_LEN,
    MAX_NAME_LEN,
    RowError,
    has_nul,
    minor_units_from_decimal_string,
    normalize_currency,
    normalize_donor_name,
    parse_sheet_datetime,
)
from donation_ledger.sources.base import SheetTable

# canonical field -> header text (compared case-insensitively, whitespace-collapsed)
REQUIRED_COLUMNS = {
    "entry_id": "entry id",
    "date": "date",
    "donor_name": "donor name",
    "donor_address": "donor address",
    "amount": "amount",
    "currency": "currency",
    "recipient": "recipient",
}


class HeaderError(Exception):
    """Required columns are missing/duplicated: the sheet layout changed. Run must fail."""


@dataclass(frozen=True)
class Candidate:
    row_number: int
    entry_id: str
    fields: dict[str, str]
    fingerprint: str
    # Either a ready-to-insert donation or the reason it is not (yet) acceptable.
    result: NewDonation | Rejection


@dataclass(frozen=True)
class Rejected:
    row_number: int
    rejection: Rejection


Classified = Candidate | Rejected


def _norm_header(text: str) -> str:
    return " ".join(text.replace("﻿", "").split()).casefold()


def map_header(header: Sequence[str]) -> dict[str, int]:
    normalised = [_norm_header(h) for h in header]
    mapping: dict[str, int] = {}
    problems: list[str] = []
    for field, expected in REQUIRED_COLUMNS.items():
        positions = [i for i, h in enumerate(normalised) if h == expected]
        if not positions:
            problems.append(f"missing column '{expected}'")
        elif len(positions) > 1:
            problems.append(f"duplicate column '{expected}'")
        else:
            mapping[field] = positions[0]
    if problems:
        raise HeaderError("; ".join(problems))
    return mapping


def _safe_cell(value: str) -> str:
    """Make arbitrary cell text storable as JSONB (no NUL, no lone surrogates)."""
    return value.replace("\x00", "\\x00").encode("utf-8", errors="replace").decode("utf-8")


def classify_table(
    table: SheetTable, tz_name: str, now: datetime | None = None
) -> list[Classified]:
    mapping = map_header(table.header)
    width = len(table.header)
    header_names = [_safe_cell(h.strip()) or f"column_{i + 1}" for i, h in enumerate(table.header)]
    parsed: list[tuple[int, list[str], dict[str, Any]]] = []
    for offset, cells in enumerate(table.rows):
        number = offset + 2  # sheet row number: header is row 1
        if all(not c.strip() for c in cells):
            continue  # fully blank spacer rows are not data
        raw = {name: _safe_cell(cells[i]) for i, name in enumerate(header_names) if i < len(cells)}
        name_header = header_names[mapping["donor_name"]]
        if raw.get(name_header, "").strip().casefold() == "anonymous":
            # The word "Anonymous" must never be stored, not even inside raw payloads.
            raw[name_header] = ""
        parsed.append((number, list(cells), raw))
    id_counts = Counter(
        cells[mapping["entry_id"]].strip()
        for _, cells, _ in parsed
        if len(cells) > mapping["entry_id"] and cells[mapping["entry_id"]].strip()
    )
    out: list[Classified] = []
    for number, cells, raw in parsed:
        out.append(_classify_row(number, cells, raw, mapping, width, id_counts, tz_name, now))
    return out


def _classify_row(
    number: int,
    cells: list[str],
    raw: dict[str, Any],
    mapping: dict[str, int],
    width: int,
    id_counts: Counter[str],
    tz_name: str,
    now: datetime | None,
) -> Classified:
    fp = fingerprint_of(raw)

    def reject(reason: str, entry_id: str | None = None) -> Rejected:
        ext = (
            entry_id if entry_id and len(entry_id) <= MAX_ID_LEN and not has_nul(entry_id) else None
        )
        return Rejected(
            number, Rejection(SOURCE_SHEET, ext, {**raw, "_row_number": number}, reason, fp)
        )

    def cell(field: str) -> str:
        i = mapping[field]
        return cells[i] if i < len(cells) else ""

    entry_id = cell("entry_id").strip()
    if len(cells) > width and any(c.strip() for c in cells[width:]):
        return reject("malformed_row: more cells than header columns", entry_id)
    if any(has_nul(c) for c in cells):
        return reject("invalid_text: contains NUL character", entry_id)
    if not entry_id:
        return reject("missing_entry_id")
    if len(entry_id) > MAX_ID_LEN:
        return reject("invalid_entry_id: too long")
    if id_counts[entry_id] > 1:
        return reject(
            f"duplicate_entry_id: appears {id_counts[entry_id]} times in the sheet", entry_id
        )
    fields = {f: _safe_cell(cell(f).strip()) for f in REQUIRED_COLUMNS}
    if fields["donor_name"].casefold() == "anonymous":
        fields["donor_name"] = ""
    result = _build_donation(entry_id, fields, raw, number, tz_name, now)
    if isinstance(result, str):
        return Candidate(
            number,
            entry_id,
            fields,
            fp,
            Rejection(SOURCE_SHEET, entry_id, {**raw, "_row_number": number}, result, fp),
        )
    return Candidate(number, entry_id, fields, fp, result)


def _build_donation(
    entry_id: str,
    fields: dict[str, str],
    raw: dict[str, Any],
    number: int,
    tz_name: str,
    now: datetime | None,
) -> NewDonation | str:
    """Return a donation, or a reason string when any field is unacceptable."""
    try:
        currency = normalize_currency(fields["currency"])
        amount = minor_units_from_decimal_string(fields["amount"], currency)
        donated_at = parse_sheet_datetime(fields["date"], tz_name, now)
        name = normalize_donor_name(fields["donor_name"])
        address = " ".join(fields["donor_address"].split()) or None
        recipient = " ".join(fields["recipient"].split()) or None
        if address is not None and len(address) > MAX_ADDRESS_LEN:
            raise RowError("invalid_address: too long")
        if recipient is not None and len(recipient) > MAX_NAME_LEN:
            raise RowError("invalid_recipient: too long")
    except RowError as exc:
        return str(exc)
    extra = {k: v for k, v in raw.items() if _norm_header(k) not in REQUIRED_COLUMNS.values()}
    return NewDonation(
        source=SOURCE_SHEET,
        external_id=entry_id,
        donor_name=name,
        donor_email=None,
        donor_address=address,
        recipient=recipient,
        amount_minor=amount,
        currency=currency,
        donated_at=donated_at,
        raw_payload={"fields": fields, "extra": extra, "sheet_row": number},
    )
