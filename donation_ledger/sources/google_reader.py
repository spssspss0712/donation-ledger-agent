"""Real Google Sheets reader.

UNVERIFIED: written against the gspread 6.x documentation but never run against a real
spreadsheet (no credentials were available). Verify in Phase B before relying on it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import gspread

from donation_ledger.sources.base import SheetReadError, SheetTable


class GoogleSheetReader:
    def __init__(self, sheet_id: str, worksheet: str | None, service_account: str) -> None:
        self._sheet_id = sheet_id
        self._worksheet = worksheet
        self._service_account = service_account

    def _credentials(self) -> dict[str, Any]:
        raw = self._service_account.strip()
        text = raw if raw.startswith("{") else Path(raw).read_text(encoding="utf-8")
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("service account JSON must be an object")
        return parsed

    def read(self) -> SheetTable:
        try:
            client = gspread.service_account_from_dict(self._credentials())
            book = client.open_by_key(self._sheet_id)
            sheet = book.worksheet(self._worksheet) if self._worksheet else book.sheet1
            # Formatted values: the same text staff see ("$1,000.50", "1/15/2024").
            values = sheet.get_all_values()
        except Exception as exc:  # noqa: BLE001 - library raises many unrelated types
            # Only the exception type: messages may echo credentials or sheet content.
            raise SheetReadError(f"cannot read Google Sheet: {type(exc).__name__}") from exc
        if not values:
            raise SheetReadError("worksheet is empty (no header row)")
        return SheetTable(header=values[0], rows=values[1:])
