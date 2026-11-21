# -*- coding: utf-8 -*-
"""FastAPI application entrypoint for the WhatsApp Cloud API handoff service."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import ValidationError

from app.engine import DEMO_CATALOG, HandoffEngine
from app.models import (
    CatalogItem,
    ConversationOut,
    ConversationSession,
    ConversationState,
    OperatorTakeoverIn,
    WebhookMessageIn,
    quantize_money,
)
from app.security import verify_hub_token, verify_meta_signature

__all__ = [
    "app",
    "create_app",
    "engine",
    "CatalogItem",
    "ConversationOut",
    "ConversationSession",
    "ConversationState",
    "DEMO_CATALOG",
    "HandoffEngine",
    "OperatorTakeoverIn",
    "WebhookMessageIn",
    "quantize_money",
]


def create_app(
    *,
    db_path: str | Path | None = None,
    bcv_rate: Decimal | None = None,
    verify_token: str | None = None,
    app_secret: str | None = None,
) -> FastAPI:
    resolved_db = db_path if db_path is not None else os.getenv("HANDOFF_DB_PATH", ":memory:")
    resolved_rate = bcv_rate if bcv_rate is not None else Decimal(os.getenv("BCV_RATE", "45.25"))
    resolved_verify = verify_token if verify_token is not None else os.getenv("WHATSAPP_VERIFY_TOKEN", "demo-verify-token")
    resolved_secret = app_secret if app_secret is not None else os.getenv("WHATSAPP_APP_SECRET", "")

    svc = HandoffEngine(
        bcv_rate=resolved_rate,
        verify_token=resolved_verify,
        app_secret=resolved_secret,
        db_path=resolved_db,
    )

    api = FastAPI(
        title="WhatsApp AI Handoff Engine",
        version="1.1.0",
        description=(
            "FastAPI webhook service for Meta WhatsApp Business Cloud API with HMAC-SHA256 verification, "
            "SQLite WAL idempotency, Decimal pricing, and deterministic human handoff."
        ),
    )
    api.state.engine = svc

    @api.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "hmac_enforced": bool(svc.app_secret),
            "bcv_rate": str(svc.bcv_rate),
        }

    @api.get("/webhook")
    def verify_meta_webhook(
        hub_mode: str = Query(..., alias="hub.mode"),
        hub_verify_token: str = Query(..., alias="hub.verify_token"),
        hub_challenge: str = Query(..., alias="hub.challenge"),
    ) -> int | str:
        if hub_mode == "subscribe" and verify_hub_token(hub_verify_token, svc.verify_token):
            return int(hub_challenge) if hub_challenge.isdigit() else hub_challenge
        raise HTTPException(status_code=403, detail="Invalid verification token")

    @api.post("/webhook/messages")
    async def receive_whatsapp_message(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    ) -> dict:
        raw_body = await request.body()
        if svc.app_secret and not verify_meta_signature(raw_body, x_hub_signature_256, svc.app_secret):
            raise HTTPException(status_code=401, detail="Invalid or missing X-Hub-Signature-256")
        try:
            payload = WebhookMessageIn.model_validate_json(raw_body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        return svc.handle_incoming(payload)

    @api.get("/conversations/{wa_id}", response_model=ConversationOut)
    def get_conversation(wa_id: str) -> ConversationOut:
        try:
            return svc.inspect_conversation(wa_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc

    @api.post("/conversations/{wa_id}/takeover")
    def operator_takeover(wa_id: str, body: OperatorTakeoverIn) -> dict:
        session = svc.assign_operator(wa_id, body.operator_id)
        return {
            "wa_id": session.wa_id,
            "state": session.state.value,
            "assigned_agent": session.assigned_agent,
        }

    @api.post("/conversations/{wa_id}/release")
    def release_to_bot(wa_id: str) -> dict:
        try:
            session = svc.close_human_ticket(wa_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        return {"wa_id": session.wa_id, "state": session.state.value}

    return api


app = create_app()
engine: HandoffEngine = app.state.engine
