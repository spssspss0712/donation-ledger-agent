"""FastAPI app: Stripe webhook plus a small read-only API."""

from __future__ import annotations

import hmac
import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from donation_ledger.config import Settings
from donation_ledger.db import make_session_factory
from donation_ledger.models import Donation, SyncRun
from donation_ledger.runs import safe_error_message
from donation_ledger.webhook import SignatureError, process_event, verify_and_parse

log = logging.getLogger(__name__)

ANONYMOUS_LABEL = "Anonymous"  # presentation only; never stored in the database


class DonationOut(BaseModel):
    """Public view. Email, address and the raw payload are deliberately not exposed."""

    id: int
    source: str
    external_id: str
    donor_name: str
    is_anonymous: bool
    recipient: str | None
    amount_minor: int
    currency: str
    donated_at: datetime
    created_at: datetime


class DonationPage(BaseModel):
    items: list[DonationOut]
    next_before_id: int | None


class SyncRunOut(BaseModel):
    id: int
    source: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    rows_read: int
    rows_inserted: int
    rows_duplicate_skipped: int
    rows_quarantined: int
    rows_ignored: int
    issues_recorded: int
    error_message: str | None


def create_app(settings: Settings, engine: Engine) -> FastAPI:
    app = FastAPI(title="donation-ledger", docs_url=None, redoc_url=None, openapi_url=None)
    session_factory = make_session_factory(engine)

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        if settings.allow_unauthenticated_api:
            return
        expected = settings.api_token
        if not expected:
            # Fail closed: donor data must not be served without a configured token.
            raise HTTPException(status_code=503, detail="API token not configured")
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.exception_handler(SQLAlchemyError)
    async def db_error_handler(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        log.error("database error", extra={"error": safe_error_message(exc)})
        return JSONResponse({"detail": "database error"}, status_code=500)

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request) -> JSONResponse:
        secret = settings.stripe_webhook_secret
        if not secret:
            log.error("stripe webhook secret is not configured")
            return JSONResponse({"detail": "webhook not configured"}, status_code=503)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_webhook_body_bytes:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        raw = await request.body()
        if len(raw) > settings.max_webhook_body_bytes:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        try:
            event = verify_and_parse(
                raw,
                request.headers.get("stripe-signature"),
                secret,
                settings.webhook_tolerance_seconds,
            )
        except SignatureError as exc:
            log.warning("stripe webhook rejected", extra={"reason": str(exc)})
            return JSONResponse({"detail": "invalid signature"}, status_code=400)
        outcome = await run_in_threadpool(process_event, session_factory, event)
        return JSONResponse({"status": outcome.value}, status_code=200)

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable", "db": "down"}, status_code=503)
        return JSONResponse({"status": "ok", "db": "ok"})

    def get_session() -> Session:
        return session_factory()

    @app.get("/donations", response_model=DonationPage, dependencies=[Depends(require_token)])
    def list_donations(
        source: Literal["stripe", "sheet"] | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before_id: Annotated[int | None, Query(ge=1)] = None,
    ) -> DonationPage:
        stmt = select(Donation).order_by(Donation.id.desc()).limit(limit + 1)
        if source is not None:
            stmt = stmt.where(Donation.source == source)
        if before_id is not None:
            stmt = stmt.where(Donation.id < before_id)
        with get_session() as session:
            rows = list(session.execute(stmt).scalars())
        page = rows[:limit]
        items = [_donation_out(d) for d in page]
        more = len(rows) > limit
        return DonationPage(items=items, next_before_id=page[-1].id if more else None)

    @app.get("/sync-runs", response_model=list[SyncRunOut], dependencies=[Depends(require_token)])
    def list_sync_runs(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        source: str | None = None,
    ) -> list[SyncRunOut]:
        stmt = select(SyncRun).order_by(SyncRun.id.desc()).limit(limit)
        if source is not None:
            stmt = stmt.where(SyncRun.source == source)
        with get_session() as session:
            return [SyncRunOut.model_validate(r, from_attributes=True) for r in session.scalars(stmt)]

    return app


def _donation_out(d: Donation) -> DonationOut:
    return DonationOut(
        id=d.id,
        source=d.source,
        external_id=d.external_id,
        donor_name=d.donor_name if d.donor_name is not None else ANONYMOUS_LABEL,
        is_anonymous=d.is_anonymous,
        recipient=d.recipient,
        amount_minor=d.amount_minor,
        currency=d.currency,
        donated_at=d.donated_at,
        created_at=d.created_at,
    )
