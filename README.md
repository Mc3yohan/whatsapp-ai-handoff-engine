# WhatsApp Cloud API + LLM Deterministic Handoff Engine

Arquitectura de referencia en **Python + FastAPI** para operar asistentes conversacionales de ventas sobre **Meta WhatsApp Business Cloud API** e **IA Generativa (Gemini / OpenAI)** en entornos comerciales reales sin alucinaciones de precios ni colisiones con operadores humanos.

> **Autor:** Víctor Yohan Betancourt Marín — [quanticosystems.com](https://quanticosystems.com)  
> *Nota: Este repositorio contiene una implementación demostrativa con catálogo 100% sintético para portafolio técnico.*

## Problemas reales de producción que resuelve
1. **Idempotencia ante reintentos de Meta (`wamid`):** Evita respuestas o cobros duplicados cuando Meta reenvía webhooks por latencia de red.
2. **Cero alucinaciones en precios y stock (`Decimal`):** Los precios en USD y su conversión multimoneda a tasa del día se calculan con aritmética `Decimal` exacta (`ROUND_HALF_UP`), nunca con `float` ni estimaciones del LLM.
3. **Máquina de estados determinista (`BOT_ACTIVE` → `HUMAN_PENDING` → `HUMAN_ACTIVE`):** Al detectar intención de pago (*Pago Móvil, Zelle, Binance, comprobante*) o solicitud de asesor, el bot transfiere la sesión y **jamás vuelve a interrumpir** mientras un humano atiende el caso.
4. **Fallo seguro (*Fail-Closed*):** Si el proveedor externo de IA presenta timeout o cuota agotada, la conversación pasa automáticamente a cola de atención humana pendiente en lugar de devolver errores crudos o falsos éxitos.

## Estructura
- `app/main.py` — API FastAPI, verificación oficial `hub.challenge` de Meta, motor de estados y endpoints de toma/liberación por operador.
- `tests/test_handoff.py` — Suite automatizada con `pytest` validando idempotencia, precisión monetaria, escalamiento y ciclo de atención humana.

## Ejecución rápida y pruebas
```bash
pip install -r requirements.txt
pytest -q
uvicorn app.main:app --reload
```
