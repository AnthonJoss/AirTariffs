# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Air fare/tariff REST API built with FastAPI (single module: `main.py`). Dependencies live in the local `.venv`.

## Run & test

- Run: `uvicorn main:app --reload --host 0.0.0.0 --port 8001` (el 8000 lo usa el contenedor next-crm-imap-service; `0.0.0.0` para que `tmsbackendnew` en Docker lo alcance vía `host.docker.internal:8001`)
- No automated tests yet. Endpoints are checked manually via `test_main.http` (PyCharm HTTP client); add a request there for every new endpoint.

## Subida de tarifas (PDF → MySQL)

- Flujo: `/tariffs-ui` sube el PDF **o Excel (.xlsx/.xlsm)** → `tariff_parser.py` (pdfplumber / `excel_reader.py` + OpenAI + perfiles por cliente en `client_profiles.py`) → pantalla de revisión → `tariff_db.insert_tariffs` (una transacción) en `tariffs`.
- Requiere `OPENAI_API_KEY` (gpt-4o) (env o `.env`). La BD usa `db_conn.py`, perfil `local` por defecto (no cambiar a `remote` sin pedirlo).
- Mapeo de tramos de peso → columnas `n/forty_five_more/hundred_more/three_hundred_more/five_hundred_more/thousand_more`: tramo más alto ≤ el requerido, si no hay el más bajo (misma regla que MailReader `analizar_egypt.py`).
- Fees (`tariff_feeds`, USD/kg): `header.fees` van a todas las filas; `rows[].fees` solo a esa fila (fees individuales del office); `fuel_fee_id` + columna fuel por fila.
- Tramos: `n` solo si el documento tiene tramo base (N/Normal/-100/1k); si empieza en `+45` queda vacío (EgyptAir: `n_from_lowest` en su perfil). `tier_headers.py` fija los `from_kg` según el encabezado (el modelo a veces corre las columnas). `extract_with_llm` cuenta las líneas de datos y reintenta si el modelo devuelve menos (`row_check`); `seed=7`.
- Un libro con una hoja por producto (Emirates): `sheets_info` marca `has_rates`/`data_rows` por hoja; el office crea una tarjeta por hoja (`sheets=[hoja]`), cada una con su commodity. `POST /tariffs/commodities` crea commodities aéreos.
- Tablas Excel grandes (`excel_table.py`): una hoja con encabezado `Min | tramos…` + columnas de origen y destino se lee SIN LLM y sin tope de filas (el LLM solo da aerolínea/vigencia/comentarios, `meta_with_llm`). Zonas de origen ("Eastern 1" = lista de `(XXX)` arriba de la tabla) se expanden a un aeropuerto por fila. `sheets_info` devuelve `table` (columnas filtrables + `matrix`) y `/tariffs/parse` acepta `row_filter` JSON `{columna: [valores]}`. `label_column` guarda en `rows[].note` el valor de esa columna (nombre del producto) y `tariff_db.row_comments` lo escribe en `tariffs.comments` de cada tarifa (`<p>•Product: …</p>`); Laravel lo lee como `productNote`. Si no se reconoce la tabla sigue el camino del LLM (tope `EXCEL_MAX_CHARS`/`MAX_EXPECTED_ROWS`).
- Excel: `excel_reader.py` (hojas visibles con datos o las de `sheets`; las filas ocultas por filtro SE leen); `POST /tariffs/excel-sheets` lista las hojas. Términos y tarifas aceptan Excel.
- Versiones por commodity (`versions_ai.py`): "súbelo también como Dangerous con mínimo 100 y +0.50/kg" en términos/instrucciones/documento → `versions[]` en el borrador; su frase se quita del prompt de filas.
- Términos por cliente (`client_terms.py`, tabla `airtariff_client_terms`, creada desde el backend): instrucciones + texto de archivos PDF/TXT/MD/CSV/Excel por `company_id`; `/tariffs/parse` los mete en el prompt (cliente elegido `client_id` o provider/airline del perfil) + `instructions` extra. `GET/POST /tariffs/terms`.
- Reglas de fees por aerolínea (`air_fee_rules`, del backend): `POST /tariffs/rules/draft` (`fee_rules_ai.py`) las propone con el LLM desde los términos guardados; no guarda nada. Las reglas no se copian a `tariff_feeds` (las aplica el backend al cotizar); el insert solo recibe fees simples por kg.
- Historial y reversa (`upload_history.py`): cada insert registra su lote (`header.batch_id`) en `airtariff_uploads` (una tabla; el servicio la crea solo la primera vez que la usa). `GET /tariffs/uploads`, `POST /tariffs/uploads/revert` (con `dry_run`), `/tariffs/uploads/edit` (corrige aerolínea/proveedor/commodity/vigencia/avión de uno o varios lotes; los de "Insert all" comparten `run_id`) y `/tariffs/uploads/rules`. Revertir borra lo no usado y **desactiva** las tarifas referenciadas por quotes/calculations/historial. Sin las tablas el insert sigue (`tracked:false`).
- Links (`sheet_links.py`, tabla `airtariff_links`, la crea el servicio): una hoja de Google publicada (`…/pubhtml`) con spots semanales (`UPDATE DATE`, `WK`, tabla `DEST | tarifa | A/C`). `POST /tariffs/links` registra y hace la primera lectura; `POST /tariffs/links/{id}/sync` ("Sync now") baja el CSV y solo si su hash cambió inserta el lote nuevo y después revierte el anterior (sin hueco). Sin IA ni cron: no somos editores de la hoja, así que no hay push; `sync-all` queda listo por si se agrega un Cloud Scheduler. Spot = tramos +300/+500/+1000 y mínimo tarifa×300; CAO→airchaft 1, PAX→2; CLOSED se omite. También lee **tarifarios completos** (`format: ratecard`): cabecera `Origin | Destination | Product Code | Product Name | Commodity Code | Routing | Min Charge | 0 | 45 | 100 | 300 | 500 | 1,000 | 2,000`; una pestaña = un link (con `#gid=`) = un commodity (el del link). Origen por fila (sin origen en el form), todos los tramos (0→`n`; el 2,000 no existe y se omite), producto como nota de la tarifa, `PAX` en el nombre → airchaft 2 si no 1, `Routing` solo informativo (no es vía) y filas idénticas repetidas por routing se cargan una vez. Importes `$19,30`/`$1,050` se normalizan (`_money`). Sin fecha en la hoja: la vigencia cuenta desde el día del sync.
- Histórico (`tariff_archive.py`, tabla `airtariff_archive`, la crea el servicio): archivar = `tariffs.active = 0` + renglón de bitácora; no se borra nada. `header.replace` en `/tariffs/insert` ({mode: same_routes|whole_airline|airline_all|batches, batch_ids, any_provider}) archiva lo que la subida nueva sustituye en la MISMA transacción del insert, sin tocar la misma tanda (`run_id`/`batch_id`). `POST /tariffs/replace/preview` (cuenta, no toca), `POST /tariffs/archive` (una tanda o tarifas sueltas a mano), `GET /tariffs/history?airline_id=` y `POST /tariffs/history/restore`. Revertir el lote que reemplazó (`upload_history.revert_batch`) restaura lo que archivó.
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
