# -*- coding: utf-8 -*-
"""Meta WhatsApp Cloud API webhook verification, HMAC-SHA256, and operator API-key checks."""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional


def verify_hub_token(provided: str, expected: str) -> bool:
    """Constant-time verification token check for Meta GET /webhook handshake."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def compute_meta_signature(raw_body: bytes, app_secret: str) -> str:
    """Compute the X-Hub-Signature-256 header value expected by Meta webhooks."""
    digest = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_meta_signature(raw_body: bytes, signature_header: Optional[str], app_secret: str) -> bool:
    """Validate X-Hub-Signature-256 in constant time (fail-closed if secret or header is empty)."""
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = compute_meta_signature(raw_body, app_secret)
    return hmac.compare_digest(signature_header.strip(), expected)


def verify_operator_api_key(provided: Optional[str], expected: str) -> bool:
    """Constant-time operator API-key check for /conversations/* management endpoints."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.strip().encode("utf-8"), expected.encode("utf-8"))
