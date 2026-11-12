# -*- coding: utf-8 -*-
from decimal import Decimal
from fastapi.testclient import TestClient
from app.main import app, HandoffEngine, WebhookMessageIn, ConversationState


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


def test_grounded_catalog_lookup_calculates_exact_decimal_ves():
    eng = HandoffEngine(bcv_rate=Decimal("45.25"))
    out = eng.handle_incoming(WebhookMessageIn(message_id="wamid.001", wa_id="584240000001", text="Precio del aceite 4T"))
    assert out["status"] == "replied"
    assert "USD 9.75" in out["bot_reply"]
    # 9.75 * 45.25 = 441.1875 -> 441.19 exact ROUND_HALF_UP
    assert "Bs. 441.19" in out["bot_reply"]


def test_idempotency_ignores_duplicate_meta_webhook_delivery():
    eng = HandoffEngine()
    msg = WebhookMessageIn(message_id="wamid.dup1", wa_id="584240000002", text="kit de transmision")
    first = eng.handle_incoming(msg)
    second = eng.handle_incoming(msg)
    assert first["status"] == "replied"
    assert second["status"] == "duplicate_ignored"
    assert second["bot_reply"] is None


def test_payment_intent_triggers_human_handoff_and_silences_bot():
    eng = HandoffEngine()
    esc = eng.handle_incoming(
        WebhookMessageIn(message_id="wamid.pay1", wa_id="584240000003", text="Quiero pagar por Pago Movil")
    )
    assert esc["status"] == "escalated_to_human"
    assert esc["state"] == ConversationState.HUMAN_PENDING.value

    # Subsequent messages must NEVER be answered by the bot while pending/assigned to human
    follow_up = eng.handle_incoming(
        WebhookMessageIn(message_id="wamid.pay2", wa_id="584240000003", text="Aqui esta la referencia 123456")
    )
    assert follow_up["status"] == "queued_for_human"
    assert follow_up["bot_reply"] is None


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


def test_operator_takeover_and_release_cycle():
    client = TestClient(app)
    wa_id = "584240000099"
    client.post(
        "/webhook/messages",
        json={"message_id": "wamid.t1", "wa_id": wa_id, "text": "Necesito hablar con un humano"},
    )
    take = client.post(f"/conversations/{wa_id}/takeover", json={"operator_id": "op_ana"})
    assert take.status_code == 200
    assert take.json()["state"] == "HUMAN_ACTIVE"

    rel = client.post(f"/conversations/{wa_id}/release")
    assert rel.status_code == 200
    assert rel.json()["state"] == "BOT_ACTIVE"
