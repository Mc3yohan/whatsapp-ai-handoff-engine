# Motor de Traspaso WhatsApp Cloud API

**Idioma:** [English (README.md)](README.md) · **Español**

[![CI](https://github.com/Mc3yohan/whatsapp-ai-handoff-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/Mc3yohan/whatsapp-ai-handoff-engine/actions/workflows/ci.yml)
[![Licencia: MIT](https://img.shields.io/badge/Licencia-MIT-38bdf8?style=flat-square)](LICENSE)

Servicio webhook en FastAPI que implementa los patrones clave detrás de asistentes comerciales sobre Meta WhatsApp Business Cloud API: **verificación HMAC-SHA256**, **idempotencia durable por `wamid` en SQLite WAL**, **conversión monetaria exacta con `Decimal`** y **máquina de estados determinista para traspaso a operador humano**.

---

## Qué Problemas Resuelve en Entornos Reales

1. **Reintentos duplicados de Meta (`wamid`):** Meta reenvía webhooks ante cualquier latencia en el ACK. `SQLiteHandoffStore.claim_message_id()` registra cada `message_id` de forma atómica (`INSERT OR IGNORE`), devolviendo `duplicate_ignored` incluso tras reiniciar el proceso.
2. **Validación criptográfica de webhooks:** Cuando `WHATSAPP_APP_SECRET` está definido, `POST /webhook/messages` valida `X-Hub-Signature-256` sobre el cuerpo crudo en tiempo constante (`hmac.compare_digest`) y rechaza firmas inválidas o ausentes con `401`.
3. **El bot nunca interrumpe al operador humano:** Al detectar intención de pago (*Pago Móvil, Zelle, Binance, comprobante*) o solicitud de asesor, la sesión pasa a `HUMAN_PENDING` / `HUMAN_ACTIVE` y el bot queda en silencio hasta que un operador la libera explícitamente (`POST /conversations/{wa_id}/release`).
4. **Cero deriva de precios ni fallos silenciosos:** Las cotizaciones USD → VES usan aritmética `Decimal` (`ROUND_HALF_UP`) y rechazan `float`. Ante un timeout del proveedor externo de IA, la conversación escala limpiamente a `HUMAN_PENDING`.

> **Alcance:** El catálogo (`DEMO_CATALOG`) es sintético y el fallo de proveedor LLM se simula mediante bandera para permitir pruebas locales y en CI sin llaves externas.

---

## Estructura

```text
app/
├── main.py       # Factoría FastAPI, /health, /webhook, /webhook/messages y rutas de operador
├── engine.py     # Máquina de estados y búsqueda en catálogo insensible a acentos
├── store.py      # Persistencia SQLite WAL thread-safe (sessions, processed_messages, audit_events)
├── security.py   # Validación constante de hub.verify_token y firma HMAC X-Hub-Signature-256
└── models.py     # Esquemas Pydantic, cuantizador Decimal y modelos de dominio
tests/
└── test_handoff.py
Dockerfile
```

---

## Inicio Rápido

### Local (Python 3.11+)
```bash
pip install -r requirements.txt
PYTHONPATH=. pytest -v

HANDOFF_DB_PATH=./data/handoff.db \
  WHATSAPP_APP_SECRET=cambia-esto \
  OPERATOR_API_KEY=clave-operador \
  uvicorn app.main:app --reload
```

### Docker
```bash
docker build -t whatsapp-handoff-engine .
docker run --rm -p 8000:8000 \
  -e WHATSAPP_VERIFY_TOKEN=demo-verify-token \
  -e WHATSAPP_APP_SECRET=cambia-esto \
  -e OPERATOR_API_KEY=clave-operador \
  whatsapp-handoff-engine
```

### Ejemplos con `curl` (Modo Demo Sin Firma)
```bash
# Mensaje entrante por webhook
curl -s -X POST http://127.0.0.1:8000/webhook/messages \
  -H "Content-Type: application/json" \
  -d '{"message_id":"wamid.demo01","wa_id":"584240000001","text":"Precio del aceite 4T"}'

# Inspección y toma por operador (requiere X-API-Key, por defecto demo-operator-key)
curl -s http://127.0.0.1:8000/conversations/584240000001 \
  -H "X-API-Key: demo-operator-key"
```

---

## Autoría y Licencia

Desarrollado por **Víctor Yohan Betancourt Marín** — [quanticosystems.com](https://quanticosystems.com).  
Publicado bajo [Licencia MIT](LICENSE).
