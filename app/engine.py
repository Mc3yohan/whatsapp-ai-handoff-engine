# -*- coding: utf-8 -*-
"""Deterministic conversation state machine and grounded catalog responder."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

from app.models import (
    CatalogItem,
    ConversationOut,
    ConversationSession,
    ConversationState,
    WebhookMessageIn,
    validate_exchange_rate,
)
from app.store import SQLiteHandoffStore


def _normalize_text(text: str) -> str:
    """Lowercase and strip diacritics so 'transmision' matches 'Transmisión'."""
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# Synthetic showcase catalog (zero client data)
DEMO_CATALOG: Dict[str, CatalogItem] = {
    "KIT-001": CatalogItem("KIT-001", "Kit de Transmisión Reforzado", Decimal("38.50"), stock=14),
    "LUB-002": CatalogItem("LUB-002", "Aceite Sintético 4T 20W50", Decimal("9.75"), stock=42),
    "BAT-003": CatalogItem("BAT-003", "Batería Gel Libre Mantenimiento 12V", Decimal("29.00"), stock=0),
}

# Stop words excluded from keyword matching so phrases like "delivery libre" do not match BAT-003
CATALOG_STOPWORDS = {"para", "libre", "reforzado", "mantenimiento", "sintetico"}

HUMAN_ESCALATION_RE = re.compile(
    r"\b(?:pagar|pago\s+movil|zelle|binance|comprobante|asesor|humano|reclamo|garantia|agent|support)\b"
)


class HandoffEngine:
    """WhatsApp Cloud API webhook processor with atomic SQLite WAL state & human-handoff locks."""

    def __init__(
        self,
        bcv_rate: Decimal = Decimal("45.25"),
        verify_token: str = "demo-verify-token",
        app_secret: str = "",
        operator_api_key: str = "demo-operator-key",
        db_path: str | Path = ":memory:",
        catalog: Optional[Dict[str, CatalogItem]] = None,
    ) -> None:
        self.bcv_rate = validate_exchange_rate(bcv_rate)
        self.verify_token = verify_token
        self.app_secret = app_secret
        self.operator_api_key = operator_api_key
        self.catalog = catalog if catalog is not None else DEMO_CATALOG
        self.store = SQLiteHandoffStore(db_path=db_path)

    def _match_catalog(self, normalized_text: str) -> list[CatalogItem]:
        query_words = set(re.findall(r"[a-z0-9\-]+", normalized_text))
        matched: list[CatalogItem] = []
        for item in self.catalog.values():
            sku_norm = _normalize_text(item.sku)
            if sku_norm in normalized_text:
                matched.append(item)
                continue
            item_tokens = {
                tok
                for tok in re.findall(r"[a-z0-9]+", _normalize_text(item.name))
                if len(tok) >= 4 and tok not in CATALOG_STOPWORDS
            }
            if query_words & item_tokens:
                matched.append(item)
        return matched

    def handle_incoming(self, payload: WebhookMessageIn) -> dict:
        with self.store.transaction():
            session = self.store.get_or_create_session(payload.wa_id)

            # 1. Atomic crash-safe idempotency guard (Meta webhooks retry on network jitter)
            if not self.store.claim_message_id(payload.message_id, payload.wa_id):
                return {
                    "status": "duplicate_ignored",
                    "state": session.state.value,
                    "bot_reply": None,
                }

            # 2. Strict boundary: bot never interrupts pending or active human sessions
            if session.state in (ConversationState.HUMAN_PENDING, ConversationState.HUMAN_ACTIVE):
                self.store.append_audit(
                    session.wa_id, session.state, "message_routed_to_human_queue", payload.text[:120]
                )
                return {
                    "status": "queued_for_human",
                    "state": session.state.value,
                    "bot_reply": None,
                }

            normalized = _normalize_text(payload.text)

            # 3. Payment / human escalation -> deterministic word-boundary handoff
            if HUMAN_ESCALATION_RE.search(normalized):
                session.state = ConversationState.HUMAN_PENDING
                session.handoff_reason = "payment_or_human_requested"
                self.store.save_session(session)
                self.store.append_audit(session.wa_id, session.state, "handoff_triggered", session.handoff_reason)
                return {
                    "status": "escalated_to_human",
                    "state": session.state.value,
                    "bot_reply": (
                        "Perfecto. Ya transferí tu conversación a un asesor humano para validar tu pago "
                        "y coordinar la entrega sin hacerte repetir los datos."
                    ),
                }

            # 4. Fail-closed safety: external LLM timeout escalates cleanly to human queue
            if payload.simulate_llm_failure:
                session.state = ConversationState.HUMAN_PENDING
                session.handoff_reason = "external_ai_provider_timeout"
                self.store.save_session(session)
                self.store.append_audit(session.wa_id, session.state, "fail_closed_handoff", session.handoff_reason)
                return {
                    "status": "fail_closed_to_human",
                    "state": session.state.value,
                    "bot_reply": (
                        "Recibimos tu mensaje. Un asesor te responderá en breve para darte información exacta."
                    ),
                }

            # 5. Grounded catalog lookup (exact Decimal pricing, accent-insensitive word matching)
            matched = self._match_catalog(normalized)
            session.prompt_tokens_used += 110
            session.completion_tokens_used += 65
            self.store.save_session(session)

            if matched:
                lines = []
                for item in matched:
                    availability = f"Disponible ({item.stock} unds)" if item.stock > 0 else "Agotado temporalmente"
                    price_bs = item.price_ves(self.bcv_rate)
                    lines.append(
                        f"- {item.name} [{item.sku}]: USD {item.price_usd} "
                        f"(Bs. {price_bs} a tasa {self.bcv_rate}) · {availability}"
                    )
                reply = "Aquí tienes la disponibilidad verificada en tiempo real:\n" + "\n".join(lines)
            else:
                reply = (
                    "Hola, soy el asistente automático. Puedes consultar disponibilidad escribiendo el nombre "
                    "del repuesto (ej. Kit de Transmisión, Aceite 4T o Batería) o escribir 'asesor' para concretar tu pago."
                )

            self.store.append_audit(session.wa_id, session.state, "bot_replied", reply[:120])
            return {
                "status": "replied",
                "state": session.state.value,
                "bot_reply": reply,
                "tokens_total": session.prompt_tokens_used + session.completion_tokens_used,
            }

    def assign_operator(self, wa_id: str, operator_id: str) -> ConversationSession:
        with self.store.transaction():
            session = self.store.get_or_create_session(wa_id)
            session.state = ConversationState.HUMAN_ACTIVE
            session.assigned_agent = operator_id
            self.store.save_session(session)
            self.store.append_audit(session.wa_id, session.state, "operator_assigned", operator_id)
            return session

    def close_human_ticket(self, wa_id: str) -> ConversationSession:
        with self.store.transaction():
            session = self.store.get_session(wa_id)
            if session is None:
                raise KeyError(wa_id)
            session.state = ConversationState.BOT_ACTIVE
            session.assigned_agent = None
            session.handoff_reason = None
            self.store.save_session(session)
            self.store.append_audit(session.wa_id, session.state, "returned_to_bot", "ticket_resolved")
            return session

    def inspect_conversation(self, wa_id: str) -> ConversationOut:
        session = self.store.get_session(wa_id)
        if session is None:
            raise KeyError(wa_id)
        return ConversationOut(
            wa_id=session.wa_id,
            state=session.state,
            assigned_agent=session.assigned_agent,
            handoff_reason=session.handoff_reason,
            tokens_total=session.prompt_tokens_used + session.completion_tokens_used,
            audit_log=self.store.get_audit_log(wa_id),
        )
