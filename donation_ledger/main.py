"""ASGI entry point: ``uvicorn donation_ledger.main:create --factory``."""

from __future__ import annotations

from fastapi import FastAPI

from donation_ledger.config import Settings
from donation_ledger.db import make_engine
from donation_ledger.logging_setup import configure_logging


def create() -> FastAPI:
    from donation_ledger.api import create_app

    configure_logging()
    settings = Settings.from_env()
    return create_app(settings, make_engine(settings.database_url))
