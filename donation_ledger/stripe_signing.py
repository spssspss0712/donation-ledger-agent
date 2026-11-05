"""Sign a payload exactly like Stripe does (used by the offline demo to make fake events).

Scheme (Stripe docs, "Verify webhook signatures"):
    Stripe-Signature: t=<unix ts>,v1=<hex HMAC-SHA256(secret, f"{t}.{raw_body}")>

The service itself never signs anything; it verifies with the Stripe SDK. The test suite has
its own independent copy (``tests/helpers.py``) and a test compares the two.
"""

from __future__ import annotations

import hashlib
import hmac
import time


def sign_payload(raw_body: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"
