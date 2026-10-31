"""Sheet readers. The sync core only depends on the SheetReader protocol."""

from __future__ import annotations

from donation_ledger.config import Settings
from donation_ledger.sources.base import SheetReader, SheetReadError, SheetTable
from donation_ledger.sources.csv_reader import CsvSheetReader

__all__ = ["CsvSheetReader", "SheetReadError", "SheetReader", "SheetTable", "make_reader"]


def make_reader(settings: Settings) -> SheetReader:
    if settings.sheet_source not in ("csv", "google"):
        raise SheetReadError("SHEET_SOURCE must be set to 'csv' or 'google'")
    if settings.sheet_source == "csv":
        return CsvSheetReader(settings.sheet_csv_path)
    # Imported lazily so offline use never needs Google libraries to load.
    from donation_ledger.sources.google_reader import GoogleSheetReader

    if not settings.google_sheet_id or not settings.google_service_account_json:
        raise SheetReadError("GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON are required")
    return GoogleSheetReader(
        settings.google_sheet_id, settings.google_worksheet, settings.google_service_account_json
    )
