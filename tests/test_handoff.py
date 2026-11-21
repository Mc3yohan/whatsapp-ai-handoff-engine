# -*- coding: utf-8 -*-
import json
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from app.main import (
    ConversationState,
    HandoffEngine,
    WebhookMessageIn,
    app,
    create_app,
    quantize_money,
)
from app.security import compute_meta_signature


def test_quantize_money_rejects_float():
    with pytest.raises(TypeError):
        quantize_money(9.75)


def test_webhook_verification_ok():
    client = TestClient(app)
    res = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "demo-verify-token", "hub.challenge": "987654"},
    )
    assert res.status_code == 200
    assert res.json() == 987654


def test_webhook_verification_rejects_bad_token():
    client = TestClient(app)
    res = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "123"},
    )
    assert res.status_code == 403


def test_hmac_signature_enforced_when_secret_configured(tmp_path):
    secret = "meta-test-secret-2026"
    secured_app = create_app(db_path=tmp_path / "hmac.db", app_secret=secret)
    client = TestClient(secured_app)

    payload = {"message_id": "wamid.hmac1", "wa_id": "584240000111", "text": "Precio del aceite"}
    raw = json.dumps(payload).encode("utf-8")

    # Missing signature -> 401
    missing = client.post("/webhook/messages", content=raw, headers={"Content-Type": "application/json"})
    assert missing.status_code == 401

    # Invalid signature -> 401
    bad = client.post(
        "/webhook/messages",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert bad.status_code == 401

    # Valid signature -> 200
    sig = compute_meta_signature(raw, secret)
    ok = client.post(
        "/webhook/messages",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "replied"


def test_grounded_catalog_lookup_calculates_exact_decimal_ves():
    eng = HandoffEngine(bcv_rate=Decimal("45.25"))
    out = eng.handle_incoming(WebhookMessageIn(message_id="wamid.001", wa_id="584240000001", text="Precio del aceite 4T"))
    assert out["status"] == "replied"
    assert "USD 9.75" in out["bot_reply"]
    # 9.75 * 45.25 = 441.1875 -> 441.19 exact ROUND_HALF_UP
    assert "Bs. 441.19" in out["bot_reply"]


def test_accent_insensitive_catalog_matching():
    eng = HandoffEngine(bcv_rate=Decimal("45.25"))
    out = eng.handle_incoming(
        WebhookMessageIn(message_id="wamid.acc1", wa_id="584240000010", text="tienen kit de transmision?")
    )
    assert out["status"] == "replied"
    assert "KIT-001" in out["bot_reply"]


def test_idempotency_and_state_survive_process_restart(tmp_path):
    db_file = tmp_path / "durable_handoff.db"
    eng1 = HandoffEngine(db_path=db_file)
    msg = WebhookMessageIn(message_id="wamid.persist1", wa_id="584240000022", text="Quiero pagar por Pago Movil")
    first = eng1.handle_incoming(msg)
    assert first["status"] == "escalated_to_human"
    assert first["state"] == ConversationState.HUMAN_PENDING.value
    eng1.store.close()

    # Simulate fresh process reading the same SQLite WAL database
    eng2 = HandoffEngine(db_path=db_file)
    dup = eng2.handle_incoming(msg)
    assert dup["status"] == "duplicate_ignored"
    assert dup["state"] == ConversationState.HUMAN_PENDING.value
    assert dup["bot_reply"] is None

    # New message still stays silenced because HUMAN_PENDING persisted
    follow = eng2.handle_incoming(
        WebhookMessageIn(message_id="wamid.persist2", wa_id="584240000022", text="Referencia 998877")
    )
    assert follow["status"] == "queued_for_human"
    assert follow["bot_reply"] is None
    eng2.store.close()


def test_fail_closed_on_llm_provider_timeout():
    eng = HandoffEngine()
    out = eng.handle_incoming(
        WebhookMessageIn(
            message_id="wamid.err1",
            wa_id="584240000004",
            text="Consulta general",
            simulate_llm_failure=True,
        )
    )
    assert out["status"] == "fail_closed_to_human"
    assert out["state"] == ConversationState.HUMAN_PENDING.value


def test_operator_takeover_audit_trail_and_release_cycle(tmp_path):
    test_app = create_app(db_path=tmp_path / "cycle.db")
    client = TestClient(test_app)
    wa_id = "584240000099"

    client.post(
        "/webhook/messages",
        json={"message_id": "wamid.t1", "wa_id": wa_id, "text": "Necesito hablar con un humano"},
    )
    take = client.post(f"/conversations/{wa_id}/takeover", json={"operator_id": "op_ana"})
    assert take.status_code == 200
    assert take.json()["state"] == "HUMAN_ACTIVE"

    inspected = client.get(f"/conversations/{wa_id}")
    assert inspected.status_code == 200
    body = inspected.json()
    assert body["assigned_agent"] == "op_ana"
    assert [e["event"] for e in body["audit_log"]] == ["handoff_triggered", "operator_assigned"]

    rel = client.post(f"/conversations/{wa_id}/release")
    assert rel.status_code == 200
    assert rel.json()["state"] == "BOT_ACTIVE"
