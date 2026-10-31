"""Offline Stripe helpers: build events and sign them exactly like Stripe does.

Signature scheme (Stripe docs, "Verify webhook signatures"):
    Stripe-Signature: t=<unix ts>,v1=<hex HMAC-SHA256(secret, f"{t}.{raw_body}")>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


def sign(raw_body: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def payment_intent(
    pi_id: str = "pi_FAKE0001",
    amount: int = 2500,
    currency: str = "usd",
    created: int = 1_735_000_000,
    **extra: Any,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": pi_id,
        "object": "payment_intent",
        "amount": amount,
        "amount_received": amount,
        "currency": currency,
        "created": created,
        "status": "succeeded",
    }
    obj.update(extra)
    return obj


def event(
    obj: dict[str, Any] | None = None,
    event_id: str = "evt_FAKE0001",
    type_: str = "payment_intent.succeeded",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "object": "event",
        "type": type_,
        "created": 1_735_000_001,
        "data": {"object": obj if obj is not None else payment_intent()},
    }


def encode(evt: dict[str, Any]) -> bytes:
    return json.dumps(evt, separators=(",", ":")).encode()
