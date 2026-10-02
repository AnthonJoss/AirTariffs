# AirTariffs

Servicio FastAPI que analiza PDFs de tarifas aéreas con IA y las inserta en
`tariffs` / `tariff_feeds` (BD `nextlog_logistic`). Lo usa el office (`office_tms`,
**Tariffs ▸ Air ▸ Add airline**) a través del backend `tmsbackendnew`
(`/api/v1/nextmind/tariff-upload/*`). Última actualización: 2026-10-02.

```
office_tms ─► tmsbackendnew (TariffUploadController, solo Admin) ─► AirTariffs (Cloud Run)
                                                                     ├─ pdfplumber + OpenAI (gpt-4o)
                                                                     └─ MySQL: tariffs, tariff_feeds, airtariff_client_terms
```

## Archivos

| Archivo | Rol |
|---|---|
| `main.py` | API (FastAPI) y middleware `X-Api-Key`. |
| `tariff_parser.py` | PDF → texto → LLM (formato compacto, cache en `cache/`) → columnas `n/w45/w100/w300/w500/w1000`. |
| `client_profiles.py` | Perfiles fijos por cliente (provider/airline, fees, fuel_fee_id, comentarios, reglas de MIN). |
| `client_terms.py` | Términos/instrucciones por cliente: extracción de texto de PDF/TXT y sección del prompt. |
| `egypt_parser.py` | Parser determinista de EgyptAir (sin LLM; si hay términos se usa el LLM). |
| `tariff_db.py` | Acceso a MySQL (catálogos, insert transaccional, términos). |
| `db_conn.py` | Conexión por perfil (`MYSQL_PROFILE` local/remote, `MYSQL_*_OVERRIDE`). |
| `gmail_client.py` | Solo local: baja PDFs de Gmail (`/mails-ui`). |

## Endpoints

Todos menos `/` exigen `X-Api-Key` cuando `TARIFF_API_KEY` está definida (Cloud Run).
Ejemplos en `test_main.http`.

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/` | Health check. |
| POST | `/tariffs/parse` | Multipart `file` (PDF), `client_id` (opcional), `instructions` (opcional) → borrador `{airline, valid_to, comments, rows[], profile, ai_context}`. No inserta. |
| GET | `/tariffs/terms?company_id=` | Términos guardados de una empresa (`instructions`, `files[]` con nombre/tamaño, `updated_by/at`). |
| POST | `/tariffs/terms` | Multipart `company_id`, `company_name`, `instructions`, `keep` (JSON con nombres a conservar), `files` (PDF/TXT/MD/CSV), `updated_by`. |
| GET | `/tariffs/companies?q=` | Empresas por nombre. |
| GET | `/tariffs/fees` | Catálogo de fees (aéreos primero). |
| GET | `/tariffs/commodities` | Catálogo de commodities. |
| POST | `/tariffs/insert` | `{header, rows}` en una transacción. |
| GET | `/tariffs-ui`, `/mails-ui`, `/mails`, `/mails/download` | Pantallas/utilidades locales. |

## Análisis de un PDF

1. `pdfplumber` extrae el texto (montos partidos como `$ 1 20.00` se reconstruyen).
2. `client_profiles.detect()` reconoce al cliente (nombre de archivo/texto).
3. **Términos:** se leen de `airtariff_client_terms` los del `client_id` recibido o, si no
   vino, los del provider/airline del perfil; más `instructions`. Van al system prompt
   (`client_terms.prompt_section`, tope 40.000 caracteres). Si la tabla no existe se
   analiza sin términos.
4. LLM → JSON compacto `rows: [[ORG, DST, min, fuel, [[desde_kg, tarifa], ...]]]`; cache
   por hash de modelo + prompt + texto.
5. Tramos → columnas: tramo más alto ≤ el requerido; si no hay, el más bajo.
6. Se marcan aeropuertos que no existen en `transports` (`unknown`).

## Insert

- `tariffs` (`type_tariff = 3`, `active = 1`) por fila, con `min + min_adjust`.
- `tariff_feeds` (USD/kg, `units = 1`): `header.fees` en todas las filas, `rows[].fees`
  solo en esa fila y el fuel de la fila como `fuel_fee_id` (FSC = 13) si es > 0.
- Todo o nada (rollback ante cualquier error).

## Tabla `airtariff_client_terms`

Una fila por `company_id`: `company_name`, `instructions`, `terms_files` (JSON
`[{name, chars, text, uploaded_at}]`), `updated_by`, timestamps. La crea el backend
(migración + `database/sql/cloud_sql_migration_2026_10_02_create_airtariff_client_terms.sql`).

## Configuración

| Variable | Local | Cloud Run |
|---|---|---|
| `OPENAI_API_KEY` | `.env` | Secreto `openai-api-key` |
| `MYSQL_PROFILE` | `local` (por defecto) | `remote` |
| `MYSQL_PASSWORD_OVERRIDE` | `.env` | Secreto `MYSQL_PASSWORD_NEXTLOG` |
| `MYSQL_USER_OVERRIDE` / `MYSQL_DB_OVERRIDE` | — | `nextlog_admin` / `nextlog_logistic` |
| `MYSQL_SOCKET_OVERRIDE` | — | `/cloudsql/fluid-guide-362602:us-east1:next-logistics-instance` |
| `MYSQL_HOST_OVERRIDE` / `MYSQL_PORT_OVERRIDE` | opcional | — |
| `TARIFF_API_KEY` | sin definir | Secreto `AIRTARIFFS_API_KEY` |
| `CORS_ORIGIN_REGEX` | localhost | localhost + `office-tms-*.run.app` |
| `TARIFF_LLM_MODEL` | `gpt-4o` | `gpt-4o` |

## Correr y desplegar

- Local: `uvicorn main:app --reload --host 0.0.0.0 --port 8001`.
- Cloud Run: `gcloud run deploy airtariffs --source . --region us-east1` (conserva env y
  secretos). Servicio: `https://airtariffs-717497920821.us-east1.run.app`.
- **Ojo:** en Cloud Run escribe en la BD de producción.
