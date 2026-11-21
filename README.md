# WhatsApp Cloud API Handoff Engine

**Language:** **English** · [Español (README.es.md)](README.es.md)

[![CI](https://github.com/Mc3yohan/whatsapp-ai-handoff-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/Mc3yohan/whatsapp-ai-handoff-engine/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-38bdf8?style=flat-square)](LICENSE)

FastAPI webhook service demonstrating the core backend patterns behind WhatsApp Business Cloud API sales assistants: **HMAC-SHA256 webhook verification**, **crash-safe `wamid` idempotency in SQLite WAL**, **exact `Decimal` currency conversion**, and a **deterministic human-handoff state machine**.

---

## Why These Patterns Matter in Real Deployments

Most WhatsApp bots break in four predictable ways once real customers start messaging:

1. **Duplicate deliveries (`wamid` retries):** Meta retries webhooks whenever network jitter delays an ACK. `SQLiteHandoffStore.claim_message_id()` records every `message_id` atomically (`INSERT OR IGNORE` on primary key), so retries return `duplicate_ignored` even across process restarts.
2. **Unsigned or spoofed webhook payloads:** When `WHATSAPP_APP_SECRET` is configured, `POST /webhook/messages` verifies `X-Hub-Signature-256` against the raw request bytes in constant time (`hmac.compare_digest`) and rejects invalid or missing signatures with `401`.
3. **Bots talking over human agents:** Once payment intent (*Pago Móvil, Zelle, Binance, comprobante*) or an agent request triggers `HUMAN_PENDING`, or an operator takes over (`HUMAN_ACTIVE`), the bot stays silent until explicitly released (`POST /conversations/{wa_id}/release`).
4. **Price hallucinations and float drift:** Catalog quotes compute USD -> VES totals with `Decimal` (`ROUND_HALF_UP`), rejecting `float` inputs outright. External LLM timeouts escalate cleanly to `HUMAN_PENDING` instead of leaking raw errors.

> **Scope note:** The catalog (`DEMO_CATALOG`) is synthetic and the external LLM call is modeled via a toggleable timeout flag so the entire webhook, persistence, HMAC, and handoff lifecycle can be tested locally with zero paid API keys.

---

## Project Layout

```text
app/
├── main.py       # FastAPI app factory, /health, /webhook, /webhook/messages, operator routes
├── engine.py     # Conversation state machine & accent-insensitive catalog responder
├── store.py      # Thread-safe SQLite WAL persistence (sessions, processed_messages, audit_events)
├── security.py   # Constant-time Meta hub.verify_token & X-Hub-Signature-256 HMAC validation
└── models.py     # Pydantic schemas, Decimal quantizer, and domain dataclasses
tests/
└── test_handoff.py
Dockerfile
```

---

## Quick Start

### Local (Python 3.11+)
```bash
pip install -r requirements.txt
PYTHONPATH=. pytest -v

# Start locally with durable SQLite WAL, Meta HMAC enforcement & operator API key
HANDOFF_DB_PATH=./data/handoff.db \
  WHATSAPP_APP_SECRET=change-me \
  OPERATOR_API_KEY=op-secret \
  uvicorn app.main:app --reload
```

### Docker
```bash
docker build -t whatsapp-handoff-engine .
docker run --rm -p 8000:8000 \
  -e WHATSAPP_VERIFY_TOKEN=demo-verify-token \
  -e WHATSAPP_APP_SECRET=change-me \
  -e OPERATOR_API_KEY=op-secret \
  whatsapp-handoff-engine
```

### Example Requests (Unsigned Demo Mode)
```bash
# Incoming webhook message
curl -s -X POST http://127.0.0.1:8000/webhook/messages \
  -H "Content-Type: application/json" \
  -d '{"message_id":"wamid.demo01","wa_id":"584240000001","text":"Precio del aceite 4T"}'

# Operator inspection & takeover (requires X-API-Key, default demo-operator-key)
curl -s http://127.0.0.1:8000/conversations/584240000001 \
  -H "X-API-Key: demo-operator-key"
```

---

## Author & License

Built by **Víctor Yohan Betancourt Marín** — [quanticosystems.com](https://quanticosystems.com).  
Released under the [MIT License](LICENSE).
