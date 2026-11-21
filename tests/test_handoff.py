# -*- coding: utf-8 -*-
import json
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from app.main import (
    ConversationState,
    HandoffEngine,
    WebhookMessageIn,
    create_app,
    quantize_money,
)
from app.security import compute_meta_signature, verify_meta_signature


def test_quantize_money_and_exchange_rate_guards():
    with pytest.raises(TypeError):
        quantize_money(9.75)
    with pytest.raises(ValueError):
        HandoffEngine(bcv_rate=Decimal("0"))
    with pytest.raises(ValueError):
        HandoffEngine(bcv_rate=Decimal("-45"))
    # Preserve 4-decimal BCV rates without premature truncation
    eng = HandoffEngine(bcv_rate=Decimal("45.2575"))
    assert eng.bcv_rate == Decimal("45.2575")


def test_webhook_verification_returns_exact_plaintext_challenge():
    with TestClient(create_app()) as client:
        for challenge in ("987654", "001234", "²", "challenge-token-abc"):
            res = client.get(
                "/webhook",
                params={"hub.mode": "subscribe", "hub.verify_token": "demo-verify-token", "hub.challenge": challenge},
            )
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("text/plain")
            assert res.text == challenge


def test_webhook_verification_rejects_bad_token():
    with TestClient(create_app()) as client:
        res = client.get(
            "/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "123"},
        )
        assert res.status_code == 403


def test_hmac_signature_fail_closed_and_enforced(tmp_path):
    assert verify_meta_signature(b"{}", "sha256=abc", "") is False

    secret = "meta-test-secret-2026"
    with TestClient(create_app(db_path=tmp_path / "hmac.db", app_secret=secret)) as client:
        payload = {"message_id": "wamid.hmac1", "wa_id": "584240000111", "text": "Precio del aceite"}
        raw = json.dumps(payload).encode("utf-8")

        assert client.post("/webhook/messages", content=raw, headers={"Content-Type": "application/json"}).status_code == 401
        assert (
            client.post(
                "/webhook/messages",
                content=raw,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
            ).status_code
            == 401
        )

        sig = compute_meta_signature(raw, secret)
        ok = client.post(
            "/webhook/messages",
            content=raw,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "replied"


def test_word_boundaries_prevent_false_escalation_and_false_catalog_hits():
    eng = HandoffEngine(bcv_rate=Decimal("45.25"))
    # "apagar" contains "pagar" as substring, must NOT trigger payment escalation
    out1 = eng.handle_incoming(
        WebhookMessageIn(message_id="wamid.wb1", wa_id="584240000031", text="Como apagar las notificaciones?")
    )
    assert out1["status"] == "replied"
    assert out1["state"] == ConversationState.BOT_ACTIVE.value

    # "libre" is a stop word, must NOT falsely match BAT-003 ("Batería Gel Libre Mantenimiento")
    out2 = eng.handle_incoming(
        WebhookMessageIn(message_id="wamid.wb2", wa_id="584240000031", text="Tienen delivery libre?")
    )
    assert out2["status"] == "replied"
    assert "BAT-003" not in out2["bot_reply"]


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

    eng2 = HandoffEngine(db_path=db_file)
    dup = eng2.handle_incoming(msg)
    assert dup["status"] == "duplicate_ignored"
    assert dup["state"] == ConversationState.HUMAN_PENDING.value
    assert dup["bot_reply"] is None

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


def test_operator_endpoints_require_api_key_and_manage_lifecycle(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "cycle.db", operator_api_key="op-secret-key")) as client:
        wa_id = "584240000099"
        client.post(
            "/webhook/messages",
            json={"message_id": "wamid.t1", "wa_id": wa_id, "text": "Necesito hablar con un humano"},
        )

        # Unauthenticated operator calls rejected (401)
        assert client.get(f"/conversations/{wa_id}").status_code == 401
        assert client.post(f"/conversations/{wa_id}/takeover", json={"operator_id": "op_ana"}).status_code == 401
        assert client.post(f"/conversations/{wa_id}/release").status_code == 401

        hdr = {"X-API-Key": "op-secret-key"}
        take = client.post(f"/conversations/{wa_id}/takeover", headers=hdr, json={"operator_id": "op_ana"})
        assert take.status_code == 200
        assert take.json()["state"] == "HUMAN_ACTIVE"

        inspected = client.get(f"/conversations/{wa_id}", headers=hdr)
        assert inspected.status_code == 200
        body = inspected.json()
        assert body["assigned_agent"] == "op_ana"
        assert [e["event"] for e in body["audit_log"]] == ["handoff_triggered", "operator_assigned"]

        rel = client.post(f"/conversations/{wa_id}/release", headers=hdr)
        assert rel.status_code == 200
        assert rel.json()["state"] == "BOT_ACTIVE"
