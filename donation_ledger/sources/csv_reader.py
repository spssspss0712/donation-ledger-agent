"""Offline sheet reader backed by a CSV export. Used by tests, demo and local runs."""

from __future__ import annotations

import csv
from pathlib import Path

from donation_ledger.sources.base import SheetReadError, SheetTable

# The csv default (128 KB) would abort the whole file on one huge cell; oversized values
# are quarantined later by field-length checks instead.
csv.field_size_limit(10_000_000)


class CsvSheetReader:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def read(self) -> SheetTable:
        try:
            # utf-8-sig: Excel/Sheets exports often start with a BOM that would corrupt
            # the first header name ("﻿Entry ID").
            # errors="replace": one stray non-UTF-8 byte must not block every other row.
            with self._path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
                records = list(csv.reader(handle))
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise SheetReadError(f"cannot read CSV: {type(exc).__name__}") from exc
        if not records:
            raise SheetReadError("CSV is empty (no header row)")
        return SheetTable(header=records[0], rows=records[1:])
