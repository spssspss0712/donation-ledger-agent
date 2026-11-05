"""Turn raw sheet cells into validated candidates or quarantine decisions. Pure logic."""

from __future__ import annotations

import unicodedata
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
    normalize_email,
    parse_sheet_datetime,
    strip_invisible,
)
from donation_ledger.sources.base import SheetTable

# canonical field -> header text (compared case-insensitively, whitespace-collapsed)
REQUIRED_COLUMNS = {
    "entry_id": "entry id",
    "date": "date",
    "donor_name": "donor name",
    "donor_email": "donor email",
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
    return " ".join(strip_invisible(text).split()).casefold()


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


def _normalize_id(cell: str) -> str:
    """NFC so an NFD re-export (e.g. from macOS) of the same ID is not a new donation."""
    return unicodedata.normalize("NFC", _safe_cell(strip_invisible(cell)))


def _safe_cell(value: str) -> str:
    """Make arbitrary cell text storable as JSONB (no NUL, no lone surrogates)."""
    return value.replace("\x00", "\\x00").encode("utf-8", errors="replace").decode("utf-8")


def classify_table(
    table: SheetTable, tz_name: str, now: datetime | None = None
) -> list[Classified]:
    mapping = map_header(table.header)
    width = len(table.header)
    header_names: list[str] = []
    for i, h in enumerate(table.header):
        name = _safe_cell(h.strip()) or f"column_{i + 1}"
        while name in header_names:  # keep both columns of a duplicated extra header
            name += f"_{i + 1}"
        header_names.append(name)
    parsed: list[tuple[int, list[str], dict[str, Any]]] = []
    for offset, cells in enumerate(table.rows):
        number = offset + 2  # sheet row number: header is row 1
        if all(not strip_invisible(c) for c in cells):
            continue  # fully blank spacer rows are not data
        raw = {name: _safe_cell(cells[i]) for i, name in enumerate(header_names) if i < len(cells)}
        name_header = header_names[mapping["donor_name"]]
        if strip_invisible(raw.get(name_header, "")).casefold() == "anonymous":
            # The word "Anonymous" must never be stored, not even inside raw payloads.
            raw[name_header] = ""
        parsed.append((number, list(cells), raw))
    id_counts = Counter(
        _normalize_id(cells[mapping["entry_id"]])
        for _, cells, _ in parsed
        if len(cells) > mapping["entry_id"] and strip_invisible(cells[mapping["entry_id"]])
    )
    out: list[Classified] = []
    seen_content: Counter[str] = Counter()
    for number, cells, raw in parsed:
        base = fingerprint_of(raw)
        n = seen_content[base]
        seen_content[base] += 1
        # Byte-identical rows are separate donations: give each its own identity so ignoring
        # or fixing one cannot silence the other.
        fp = base if n == 0 else fingerprint_of({**raw, "_occurrence": n})
        out.append(_classify_row(number, cells, raw, mapping, width, id_counts, tz_name, now, fp))
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
    fp: str,
) -> Classified:

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

    entry_id = _normalize_id(cell("entry_id"))
    if len(cells) > width and any(strip_invisible(c) for c in cells[width:]):
        return reject("malformed_row: more cells than header columns", entry_id)
    if len(cells) < width:
        # A cut-off line (interrupted export) would otherwise parse as a smaller donation
        # that the append-only ledger could never correct. Readers must pad blank cells.
        return reject("malformed_row: fewer cells than header columns", entry_id)
    # The email cell is exempt from both encoding checks below: a bad email must never
    # quarantine a donation (SPEC 2.3). It is stored as NULL with an invalid_email warning.
    email_at = mapping["donor_email"]
    checked = [c for i, c in enumerate(cells) if i != email_at]
    if any("\ufffd" in c for c in checked):
        # Produced by the CSV reader for undecodable bytes: storing it would be permanent
        # corruption, and a later UTF-8 re-export would then insert the donation twice.
        return reject("invalid_text: undecodable characters (is the file UTF-8?)", entry_id)
    if any(has_nul(c) for c in checked):
        return reject("invalid_text: contains NUL character", entry_id)
    if not entry_id:
        return reject("missing_entry_id")
    if len(entry_id) > MAX_ID_LEN:
        return reject("invalid_entry_id: too long")
    if id_counts[entry_id] > 1:
        return reject(
            f"duplicate_entry_id: appears {id_counts[entry_id]} times in the sheet", entry_id
        )
    fields = {f: _safe_cell(strip_invisible(cell(f))) for f in REQUIRED_COLUMNS}
    fields["entry_id"] = entry_id  # normalised form, so re-encoded IDs raise no warning
    if fields["donor_name"].casefold() == "anonymous":
        fields["donor_name"] = ""
    result = _build_donation(entry_id, fields, cell("donor_email"), raw, number, tz_name, now)
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
    email_cell: str,
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
        truncated: list[str] = []
        name_text = " ".join(fields["donor_name"].split())
        if len(name_text) > MAX_NAME_LEN:
            name_text = _cut(name_text, MAX_NAME_LEN, "donor_name", truncated) or ""
        name = normalize_donor_name(name_text)
        # Contact fields never quarantine a row: over-long text is cut and flagged instead.
        address = _cut(
            " ".join(fields["donor_address"].split()) or None,
            MAX_ADDRESS_LEN,
            "donor_address",
            truncated,
        )
        recipient = _cut(
            " ".join(fields["recipient"].split()) or None, MAX_NAME_LEN, "recipient", truncated
        )
        email = normalize_email(email_cell)
    except RowError as exc:
        return str(exc)
    except (ArithmeticError, ValueError, UnicodeError):
        return "invalid_row: value could not be processed"
    extra = {k: v for k, v in raw.items() if _norm_header(k) not in REQUIRED_COLUMNS.values()}
    return NewDonation(
        source=SOURCE_SHEET,
        external_id=entry_id,
        donor_name=name,
        donor_email=email.email,
        invalid_email=email.invalid_text,
        truncated=tuple(truncated),
        donor_address=address,
        recipient=recipient,
        amount_minor=amount,
        currency=currency,
        donated_at=donated_at,
        raw_payload={"fields": fields, "extra": extra, "sheet_row": number},
    )


def _cut(value: str | None, limit: int, field: str, truncated: list[str]) -> str | None:
    """Deterministic cut, so a re-read of the same cell equals the stored value."""
    if value is None or len(value) <= limit:
        return value
    truncated.append(field)
    return value[:limit].rstrip()
