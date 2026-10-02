# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Air fare/tariff REST API built with FastAPI (single module: `main.py`). Dependencies live in the local `.venv`.

## Run & test

- Run: `uvicorn main:app --reload --host 0.0.0.0 --port 8001` (el 8000 lo usa el contenedor next-crm-imap-service; `0.0.0.0` para que `tmsbackendnew` en Docker lo alcance vía `host.docker.internal:8001`)
- No automated tests yet. Endpoints are checked manually via `test_main.http` (PyCharm HTTP client); add a request there for every new endpoint.

## Subida de tarifas (PDF → MySQL)

- Flujo: `/tariffs-ui` sube el PDF → `tariff_parser.py` (pdfplumber + OpenAI + perfiles por cliente en `client_profiles.py`) → pantalla de revisión → `tariff_db.insert_tariffs` (una transacción) en `tariffs`.
- Requiere `OPENAI_API_KEY` (gpt-4o) (env o `.env`). La BD usa `db_conn.py`, perfil `local` por defecto (no cambiar a `remote` sin pedirlo).
- Mapeo de tramos de peso → columnas `n/forty_five_more/hundred_more/three_hundred_more/five_hundred_more/thousand_more`: tramo más alto ≤ el requerido, si no hay el más bajo (misma regla que MailReader `analizar_egypt.py`).
- Gmail: `python gmail_client.py` autoriza una vez; `/mails-ui` baja PDFs a `pdfs/`.

## Velocidad del análisis

- EgyptAir se analiza sin LLM con `egypt_parser.py` (determinista, instantáneo); si el formato no cuadra devuelve `None` y se usa el LLM.
- El LLM (ITA, Next Logistics, clientes nuevos) responde en formato compacto y su resultado se cachea en `cache/` por hash del texto: repetir un PDF es instantáneo.
- `office_tms` no llama a uvicorn directo: pasa por `tmsbackendnew` (`TariffUploadController`, `/api/v1/nextmind/tariff-upload/*`, solo Admin), que apunta a `TARIFF_UPLOAD_SERVICE_URL`.

## Cloud Run

- Servicio `airtariffs` (us-east1, proyecto `fluid-guide-362602`): https://airtariffs-717497920821.us-east1.run.app — mismo patrón que `next-notice-carrier`.
- Redeploy: `gcloud run deploy airtariffs --source . --region us-east1` (conserva env vars y secretos).
- BD: `MYSQL_PROFILE=remote` + `MYSQL_SOCKET_OVERRIDE=/cloudsql/fluid-guide-362602:us-east1:next-logistics-instance` (ver `db_conn.py`). En local el perfil sigue siendo `local` y la contraseña va en `.env` como `MYSQL_PASSWORD_OVERRIDE`.
- Secret Manager: `OPENAI_API_KEY`←`openai-api-key`, `MYSQL_PASSWORD_OVERRIDE`←`MYSQL_PASSWORD_NEXTLOG`, `TARIFF_API_KEY`←`AIRTARIFFS_API_KEY`.
- Con `TARIFF_API_KEY` definida todo salvo `/` exige `X-Api-Key`. tmsbackendnew lo manda desde `TARIFF_UPLOAD_API_KEY` (mismo secreto). CORS en `CORS_ORIGIN_REGEX` (incluye office-tms en run.app).
