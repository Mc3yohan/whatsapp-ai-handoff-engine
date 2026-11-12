# -*- coding: utf-8 -*-
"""
WhatsApp Cloud API + LLM Guardrails & Deterministic Human Handoff Engine (Demo)
Author: Víctor Yohan Betancourt Marín (https://quanticosystems.com)

Clean-room reference architecture demonstrating production patterns for:
1. Meta WhatsApp Business Cloud API webhook verification & idempotent message intake.
2. Deterministic conversation state machine (BOT_ACTIVE -> HUMAN_PENDING -> HUMAN_ACTIVE).
3. Zero-hallucination catalog lookups using Decimal money invariants.
4. Token budget tracking and automatic fail-closed escalation to human operators on external LLM errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field


class ConversationState(str, Enum):
    BOT_ACTIVE = "BOT_ACTIVE"
    HUMAN_PENDING = "HUMAN_PENDING"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"


@dataclass(frozen=True)
class CatalogItem:
    sku: str
    name: str
    price_usd: Decimal
    stock: int

    def price_ves(self, bcv_rate: Decimal) -> Decimal:
        return (self.price_usd * bcv_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class ConversationSession:
    wa_id: str
    state: ConversationState = ConversationState.BOT_ACTIVE
    assigned_agent: Optional[str] = None
    handoff_reason: Optional[str] = None
    processed_message_ids: set[str] = field(default_factory=set)
    prompt_tokens_used: int = 0
    completion_tokens_used: int = 0
    audit_log: List[dict] = field(default_factory=list)

    def log_event(self, event: str, detail: str) -> None:
        self.audit_log.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "state": self.state.value,
                "event": event,
                "detail": detail,
            }
        )


# Synthetic showcase catalog (zero real client data)
DEMO_CATALOG: Dict[str, CatalogItem] = {
    "KIT-001": CatalogItem("KIT-001", "Kit de Transmisión Reforzado", Decimal("38.50"), stock=14),
    "LUB-002": CatalogItem("LUB-002", "Aceite Sintético 4T 20W50", Decimal("9.75"), stock=42),
    "BAT-003": CatalogItem("BAT-003", "Batería Gel Libre Mantenimiento 12V", Decimal("29.00"), stock=0),
}

HUMAN_ESCALATION_KEYWORDS = (
    "pagar",
    "pago movil",
    "zelle",
    "binance",
    "comprobante",
    "asesor",
    "humano",
    "reclamo",
    "garantia",
)


class WebhookMessageIn(BaseModel):
    message_id: str = Field(..., min_length=3)
    wa_id: str = Field(..., min_length=8)
    text: str = Field(..., min_length=1)
    simulate_llm_failure: bool = False


class OperatorTakeoverIn(BaseModel):
    operator_id: str = Field(..., min_length=2)


class HandoffEngine:
    def __init__(self, bcv_rate: Decimal = Decimal("45.25"), verify_token: str = "demo-verify-token") -> None:
        self.bcv_rate = bcv_rate
        self.verify_token = verify_token
        self.sessions: Dict[str, ConversationSession] = {}

    def get_or_create_session(self, wa_id: str) -> ConversationSession:
        if wa_id not in self.sessions:
            self.sessions[wa_id] = ConversationSession(wa_id=wa_id)
        return self.sessions[wa_id]

    def handle_incoming(self, payload: WebhookMessageIn) -> dict:
        session = self.get_or_create_session(payload.wa_id)

        # 1. Idempotency guard (Meta webhooks retry on network jitter)
        if payload.message_id in session.processed_message_ids:
            return {
                "status": "duplicate_ignored",
                "state": session.state.value,
                "bot_reply": None,
            }
        session.processed_message_ids.add(payload.message_id)

        # 2. Strict boundary: Bot NEVER interrupts pending/active human sessions
        if session.state in (ConversationState.HUMAN_PENDING, ConversationState.HUMAN_ACTIVE):
            session.log_event("message_routed_to_human_queue", payload.text)
            return {
                "status": "queued_for_human",
                "state": session.state.value,
                "bot_reply": None,
            }

        normalized = payload.text.strip().lower()

        # 3. Payment / special-case intent -> clean deterministic handoff
        if any(kw in normalized for kw in HUMAN_ESCALATION_KEYWORDS):
            session.state = ConversationState.HUMAN_PENDING
            session.handoff_reason = "payment_or_human_requested"
            session.log_event("handoff_triggered", session.handoff_reason)
            return {
                "status": "escalated_to_human",
                "state": session.state.value,
                "bot_reply": (
                    "Perfecto. Ya transferí tu conversación a un asesor humano para validar tu pago "
                    "y coordinar la entrega sin hacerte repetir los datos."
                ),
            }

        # 4. Fail-closed safety: external LLM/API failure becomes pending human attention, never false success
        if payload.simulate_llm_failure:
            session.state = ConversationState.HUMAN_PENDING
            session.handoff_reason = "external_ai_provider_timeout"
            session.log_event("fail_closed_handoff", session.handoff_reason)
            return {
                "status": "fail_closed_to_human",
                "state": session.state.value,
                "bot_reply": (
                    "Recibimos tu mensaje. Un asesor te responderá en breve para darte información exacta."
                ),
            }

        # 5. Grounded catalog lookup (zero hallucinated prices or stock)
        matched = [
            item
            for item in DEMO_CATALOG.values()
            if item.sku.lower() in normalized
            or any(word in normalized for word in item.name.lower().split() if len(word) > 3)
        ]
        session.prompt_tokens_used += 110
        session.completion_tokens_used += 65

        if matched:
            lines = []
            for item in matched:
                availability = f"Disponible ({item.stock} unds)" if item.stock > 0 else "Agotado temporalmente"
                price_bs = item.price_ves(self.bcv_rate)
                lines.append(
                    f"- {item.name} [{item.sku}]: USD {item.price_usd} (Bs. {price_bs} a tasa {self.bcv_rate}) · {availability}"
                )
            reply = "Aquí tienes la disponibilidad verificada en tiempo real:\n" + "\n".join(lines)
        else:
            reply = (
                "Hola, soy el asistente automático. Puedes consultar disponibilidad escribiendo el nombre "
                "del repuesto (ej. Kit de Transmisión, Aceite 4T o Batería) o escribir 'asesor' para concretar tu pago."
            )

        session.log_event("bot_replied", reply[:80])
        return {
            "status": "replied",
            "state": session.state.value,
            "bot_reply": reply,
            "tokens_total": session.prompt_tokens_used + session.completion_tokens_used,
        }

    def assign_operator(self, wa_id: str, operator_id: str) -> ConversationSession:
        session = self.get_or_create_session(wa_id)
        session.state = ConversationState.HUMAN_ACTIVE
        session.assigned_agent = operator_id
        session.log_event("operator_assigned", operator_id)
        return session

    def close_human_ticket(self, wa_id: str) -> ConversationSession:
        if wa_id not in self.sessions:
            raise KeyError(wa_id)
        session = self.sessions[wa_id]
        session.state = ConversationState.BOT_ACTIVE
        session.assigned_agent = None
        session.handoff_reason = None
        session.log_event("returned_to_bot", "ticket_resolved")
        return session


engine = HandoffEngine()
app = FastAPI(
    title="WhatsApp AI Handoff Engine",
    version="1.0.0",
    description="Production-grade webhook & state-machine architecture for WhatsApp Cloud API + LLMs.",
)


@app.get("/webhook")
def verify_meta_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
) -> int:
    if hub_mode == "subscribe" and hub_verify_token == engine.verify_token:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Invalid verification token")


@app.post("/webhook/messages")
def receive_whatsapp_message(payload: WebhookMessageIn) -> dict:
    return engine.handle_incoming(payload)


@app.post("/conversations/{wa_id}/takeover")
def operator_takeover(wa_id: str, body: OperatorTakeoverIn) -> dict:
    session = engine.assign_operator(wa_id, body.operator_id)
    return {
        "wa_id": session.wa_id,
        "state": session.state.value,
        "assigned_agent": session.assigned_agent,
    }


@app.post("/conversations/{wa_id}/release")
def release_to_bot(wa_id: str) -> dict:
    try:
        session = engine.close_human_ticket(wa_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    return {"wa_id": session.wa_id, "state": session.state.value}
