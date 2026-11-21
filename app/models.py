# -*- coding: utf-8 -*-
"""Domain models and strict Decimal helpers for the WhatsApp handoff service."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

TWOPLACES = Decimal("0.01")


def quantize_money(value: Decimal | str | int) -> Decimal:
    """Quantize monetary values using ROUND_HALF_UP and explicitly reject float."""
    if isinstance(value, float):
        raise TypeError("Floats are forbidden for monetary calculations; use Decimal or str.")
    return Decimal(value).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


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
        return quantize_money(self.price_usd * bcv_rate)


@dataclass
class ConversationSession:
    wa_id: str
    state: ConversationState = ConversationState.BOT_ACTIVE
    assigned_agent: Optional[str] = None
    handoff_reason: Optional[str] = None
    prompt_tokens_used: int = 0
    completion_tokens_used: int = 0
    updated_at: str = ""


class WebhookMessageIn(BaseModel):
    message_id: str = Field(..., min_length=3, max_length=128)
    wa_id: str = Field(..., min_length=8, max_length=32)
    text: str = Field(..., min_length=1, max_length=4096)
    simulate_llm_failure: bool = False


class OperatorTakeoverIn(BaseModel):
    operator_id: str = Field(..., min_length=2, max_length=64)


class AuditEventOut(BaseModel):
    ts: str
    state: str
    event: str
    detail: str


class ConversationOut(BaseModel):
    wa_id: str
    state: ConversationState
    assigned_agent: Optional[str] = None
    handoff_reason: Optional[str] = None
    tokens_total: int
    audit_log: List[AuditEventOut]
