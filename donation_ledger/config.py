"""Settings read from environment variables only (never from files in the repo)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_BATCH_SIZE = 5000


class ConfigError(Exception):
    """Raised when the environment is missing or contains unsafe configuration."""


def normalize_database_url(url: str) -> str:
    """Force the psycopg 3 driver; Render and Heroku hand out ``postgres://`` URLs."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@dataclass(frozen=True)
class Settings:
    database_url: str
    stripe_webhook_secret: str | None = None
    stripe_api_key: str | None = None
    sheet_timezone: str = "America/Los_Angeles"
    sheet_source: str = "csv"  # dataclass default only; from_env requires an explicit value
    sheet_csv_path: str = "sample_data/sheet.csv"
    google_sheet_id: str | None = None
    google_worksheet: str | None = None
    google_service_account_json: str | None = None
    sync_batch_size: int = 500
    reconcile_lookback_hours: int = 72
    api_token: str | None = None
    allow_unauthenticated_api: bool = False
    webhook_tolerance_seconds: int = 300
    max_webhook_body_bytes: int = 1_000_000

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env

        def opt(name: str) -> str | None:
            value = e.get(name, "").strip()
            return value or None

        database_url = opt("DATABASE_URL")
        if database_url is None:
            raise ConfigError("DATABASE_URL is required")
        api_key = opt("STRIPE_API_KEY")
        # Hard rule of this project: live keys are never accepted.
        if api_key is not None and not api_key.startswith(("sk_test_", "rk_test_")):
            raise ConfigError("STRIPE_API_KEY must be a test-mode key (sk_test_ / rk_test_)")
        tz = opt("SHEET_TIMEZONE") or cls.sheet_timezone
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"SHEET_TIMEZONE is not a valid IANA zone: {tz!r}") from exc
        # No default on purpose: forgetting it must not silently sync the bundled sample CSV
        # (fake donors) into a real ledger.
        source = (opt("SHEET_SOURCE") or "").lower()
        if source not in ("csv", "google"):
            raise ConfigError("SHEET_SOURCE must be set to 'csv' or 'google'")
        return cls(
            database_url=normalize_database_url(database_url),
            stripe_webhook_secret=opt("STRIPE_WEBHOOK_SECRET"),
            stripe_api_key=api_key,
            sheet_timezone=tz,
            sheet_source=source,
            sheet_csv_path=opt("SHEET_CSV_PATH") or cls.sheet_csv_path,
            google_sheet_id=opt("GOOGLE_SHEET_ID"),
            google_worksheet=opt("GOOGLE_WORKSHEET"),
            google_service_account_json=opt("GOOGLE_SERVICE_ACCOUNT_JSON"),
            sync_batch_size=_positive_int(e, "SYNC_BATCH_SIZE", cls.sync_batch_size),
            reconcile_lookback_hours=_positive_int(
                e, "RECONCILE_LOOKBACK_HOURS", cls.reconcile_lookback_hours
            ),
            api_token=opt("API_TOKEN"),
            allow_unauthenticated_api=(e.get("ALLOW_UNAUTHENTICATED_API", "").lower() == "true"),
        )


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < 1:
        raise ConfigError(f"{name} must be >= 1")
    if name == "RECONCILE_LOOKBACK_HOURS" and value > 24 * 365:
        raise ConfigError("RECONCILE_LOOKBACK_HOURS must be <= 8760 (one year)")
    if name == "SYNC_BATCH_SIZE" and value > MAX_BATCH_SIZE:
        # One multi-row INSERT binds ~11 parameters per row; PostgreSQL allows 65535.
        raise ConfigError(f"SYNC_BATCH_SIZE must be <= {MAX_BATCH_SIZE}")
    return value
