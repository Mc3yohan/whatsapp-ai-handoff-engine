# -*- coding: utf-8 -*-
"""FastAPI application entrypoint for the WhatsApp Cloud API handoff service."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
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
    validate_exchange_rate,
)
from app.security import verify_hub_token, verify_meta_signature, verify_operator_api_key

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
    "validate_exchange_rate",
]


def create_app(
    *,
    db_path: str | Path | None = None,
    bcv_rate: Decimal | None = None,
    verify_token: str | None = None,
    app_secret: str | None = None,
    operator_api_key: str | None = None,
) -> FastAPI:
    resolved_db = db_path if db_path is not None else os.getenv("HANDOFF_DB_PATH", ":memory:")
    resolved_rate = bcv_rate if bcv_rate is not None else Decimal(os.getenv("BCV_RATE", "45.25"))
    resolved_verify = verify_token if verify_token is not None else os.getenv("WHATSAPP_VERIFY_TOKEN", "demo-verify-token")
    resolved_secret = app_secret if app_secret is not None else os.getenv("WHATSAPP_APP_SECRET", "")
    resolved_op_key = (
        operator_api_key if operator_api_key is not None else os.getenv("OPERATOR_API_KEY", "demo-operator-key")
    )

    svc = HandoffEngine(
        bcv_rate=resolved_rate,
        verify_token=resolved_verify,
        app_secret=resolved_secret,
        operator_api_key=resolved_op_key,
        db_path=resolved_db,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        svc.store.close()

    api = FastAPI(
        title="WhatsApp AI Handoff Engine",
        version="1.2.0",
        description=(
            "FastAPI webhook service for Meta WhatsApp Business Cloud API with HMAC-SHA256 verification, "
            "atomic SQLite WAL idempotency, Decimal pricing, API-key operator routes, and human handoff."
        ),
        lifespan=lifespan,
    )
    api.state.engine = svc

    def _require_operator(x_api_key: str | None) -> None:
        if not verify_operator_api_key(x_api_key, svc.operator_api_key):
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    @api.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "hmac_enforced": bool(svc.app_secret),
            "bcv_rate": str(svc.bcv_rate),
        }

    @api.get("/webhook", response_class=PlainTextResponse)
    def verify_meta_webhook(
        hub_mode: str = Query(..., alias="hub.mode"),
        hub_verify_token: str = Query(..., alias="hub.verify_token"),
        hub_challenge: str = Query(..., alias="hub.challenge"),
    ) -> PlainTextResponse:
        if hub_mode == "subscribe" and verify_hub_token(hub_verify_token, svc.verify_token):
            return PlainTextResponse(content=hub_challenge)
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
        return await run_in_threadpool(svc.handle_incoming, payload)

    @api.get("/conversations/{wa_id}", response_model=ConversationOut)
    def get_conversation(
        wa_id: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ConversationOut:
        _require_operator(x_api_key)
        try:
            return svc.inspect_conversation(wa_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc

    @api.post("/conversations/{wa_id}/takeover")
    def operator_takeover(
        wa_id: str,
        body: OperatorTakeoverIn,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        _require_operator(x_api_key)
        session = svc.assign_operator(wa_id, body.operator_id)
        return {
            "wa_id": session.wa_id,
            "state": session.state.value,
            "assigned_agent": session.assigned_agent,
        }

    @api.post("/conversations/{wa_id}/release")
    def release_to_bot(
        wa_id: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict:
        _require_operator(x_api_key)
        try:
            session = svc.close_human_ticket(wa_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        return {"wa_id": session.wa_id, "state": session.state.value}

    return api


app = create_app()
engine: HandoffEngine = app.state.engine
