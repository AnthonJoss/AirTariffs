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
| `tariff_parser.py` | PDF o Excel → texto → LLM (formato compacto, cache en `cache/`) → columnas `n/w45/w100/w300/w500/w1000`. |
| `excel_reader.py` | Excel (.xlsx/.xlsm) → texto (una fila por línea, celdas con ` \| `): hojas, filas filtradas, tope de caracteres. |
| `upload_history.py` | Historial de uploads (lote por archivo) y su reversa: lista, revierte (borra o desactiva) y asocia reglas de fees. |
| `tier_headers.py` | Lee el encabezado de la tabla (`Min +45 +100 …`) y fija los tramos de peso de cada fila: el modelo a veces inventa un tramo base o corre las columnas. |
| `versions_ai.py` | Detecta "súbelo también como otro commodity" (mínimo y +USD/kg o %) en términos, instrucciones o documento. |
| `client_profiles.py` | Perfiles fijos por cliente (provider/airline, fees, fuel_fee_id, comentarios, reglas de MIN). |
| `client_terms.py` | Términos/instrucciones por cliente: extracción de texto de PDF/TXT/MD/CSV/Excel y sección del prompt. |
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
| POST | `/tariffs/parse` | Multipart `file` (**PDF o Excel .xlsx/.xlsm**), `client_id` (opcional), `instructions` (opcional), `sheets` (opcional, solo Excel: JSON con las hojas a leer) → borrador `{airline, valid_to, comments, rows[], profile, ai_context, versions[], excel{sheets,used}}`. No inserta. |
| POST | `/tariffs/excel-sheets` | Multipart `file` (Excel) → `{sheets:[{name, visible, rows, hidden_rows, chars, data_rows, has_rates, selected}]}` (`has_rates`/`data_rows`: la hoja es una tabla de tarifas por tramos de peso y cuántas líneas de datos tiene) para elegir qué hojas leer. No analiza. |
| GET | `/tariffs/terms?company_id=` | Términos guardados de una empresa (`instructions`, `files[]` con nombre/tamaño, `updated_by/at`). |
| POST | `/tariffs/terms` | Multipart `company_id`, `company_name`, `instructions`, `keep` (JSON con nombres a conservar), `files` (PDF/TXT/MD/CSV/Excel), `sheets` (JSON `{archivo: [hojas]}`), `updated_by`. |
| POST | `/tariffs/rules/draft` | JSON `{airline_id, company_ids[], text}` → `{rules[], skipped[], sources}`: reglas de fees propuestas por el LLM desde los términos guardados (`fee_rules_ai.py`, cache por hash). No guarda. |
| GET | `/tariffs/companies?q=` | Empresas por nombre. |
| GET | `/tariffs/fees` | Catálogo de fees (aéreos primero). |
| GET | `/tariffs/commodities` | Catálogo de commodities de carga aérea (`mode = 'air'`). |
| POST | `/tariffs/commodities` | `{name}`: crea un commodity aéreo o devuelve el existente (para hojas de productos que no están en el catálogo). |
| POST | `/tariffs/insert` | `{header, rows}` en una transacción. `header.batch_id`, `header.source_file` y `header.uploaded_by` registran el lote; responde `{tariffs, fees, batch_id, tracked}`. |
| GET | `/tariffs/uploads?limit=` | Últimos uploads agrupados por lote: `{tracking, uploads[]}`. |
| POST | `/tariffs/uploads/revert` | `{batch_id, reverted_by, dry_run}` → `{deleted_tariffs, deleted_fees, deactivated_tariffs, already_gone, deleted_rules, rule_ids}`. 404 / 409 (ya revertido) / 503 (sin tabla). |
| POST | `/tariffs/uploads/edit` | `{batch_ids[], changes, edited_by, dry_run}`: cambia `airline_id`/`provider_id`/`commodity_id`/`valid_to`/`airchaft` de todas las tarifas de esos uploads (corregir una tanda capturada mal). Actualiza también las `air_fee_rules` del upload (aerolínea y `provider_in`/`commodity_in`). Devuelve `{uploads, tariffs, rules, duplicates, changes}` con nombres; 404 / 422 (revertido, varios commodities, valores inválidos). |
| POST | `/tariffs/uploads/rules` | `{batch_id, rule_ids[]}`: reglas de fees creadas por el upload (se borran al revertir). |
| GET | `/tariffs-ui`, `/mails-ui`, `/mails`, `/mails/download` | Pantallas/utilidades locales. |

## Excel

`excel_reader.py` lee las hojas **visibles con datos** (o las que se pidan en `sheets`, también ocultas):
una fila por línea, celdas separadas por ` | `, valores calculados (`data_only`). Las **filas y columnas
ocultas por un filtro se leen siempre** (el filtro es una vista). Tope `TARIFF_EXCEL_MAX_CHARS`
(60.000): con hojas por defecto las que no caben se omiten; con hojas elegidas, si no caben es un error.
Un Excel entra al mismo flujo que el PDF (perfil, términos, cache). Cada petición de `/tariffs/parse` guarda el archivo en su **propia carpeta temporal** (se borra al terminar): varias tarjetas del mismo libro (una por hoja) llegan a la vez con el mismo nombre y compartir la ruta daba `BadZipFile: Truncated file header`. El parser fijo de EgyptAir lee
líneas de PDF: con un Excel cae al LLM.

## Reversa de un upload

Cada insert registra su lote en la tabla `airtariff_uploads` (ids de tarifas en `tariff_ids`, JSON) **en la misma transacción**. El servicio **crea la tabla solo** la primera vez que la usa (`ensure_table`, `CREATE TABLE IF NOT EXISTS`); si no tiene permiso para crearla el insert sigue (`tracked: false`). `revert_batch` borra las tarifas y sus `tariff_feeds` del lote, salvo las que ya referencia `quotes`, `calculations`, `history_instant_air_result_feeds` o otra tarifa (`clone_tariff_id`, `autogenerated_tariff_id`, `one_tariff_id`, `two_tariff_id`): esas se **desactivan** (`active = 0`). También borra las `air_fee_rules` asociadas. Un lote se revierte una vez; `dry_run` solo cuenta.

## Editar un upload ya insertado

`upload_history.edit_batches` corrige aerolínea / proveedor / commodity / vigencia / tipo de avión en **todas** las tarifas de uno o varios lotes (una tanda de PDFs con un dato mal elegido), sin borrar y volver a subir. Los PDFs insertados juntos ("Insert all") comparten `run_id`. El commodity solo cambia si el lote no tiene versiones; un lote revertido no se edita. `duplicates` cuenta las tarifas que con el cambio coincidirían con otra vigente que no es de la selección. Cada edición se guarda en `edit_log` (quién, cuándo y de → a). `ensure_table` agrega las columnas nuevas (`run_id`, `edited_at`, `edited_by`, `edit_log`) a una tabla ya creada.

## Versiones por commodity

`versions_ai.detect` busca (solo si el texto menciona algo parecido) una orden explícita de subir las
mismas tarifas con otro commodity: `[{commodity, mode: surcharge|pct|same, amount, min, source_text}]`.
La frase se quita de lo que lee el LLM de las filas (`strip_sources`) para no confundirlo. El office la
deja en "Also upload as another commodity" con la etiqueta *From the terms*. Si falla no se pierde el
análisis (`versions_error`).

## Análisis de un PDF

1. `pdfplumber` extrae el texto del PDF (montos partidos como `$ 1 20.00` se reconstruyen); un Excel pasa por `excel_reader`.
2. `client_profiles.detect()` reconoce al cliente (nombre de archivo/texto).
3. **Términos:** se leen de `airtariff_client_terms` los del `client_id` recibido o, si no
   vino, los del provider/airline del perfil; más `instructions`. Van al system prompt
   (`client_terms.prompt_section`, tope 40.000 caracteres). Si la tabla no existe se
   analiza sin términos.
4. LLM → JSON compacto `rows: [[ORG, DST, min, fuel, [[desde_kg, tarifa], ...]]]`; cache
   por hash de modelo + prompt + texto.
5. Tramos → columnas: tramo más alto ≤ el requerido; si no hay, el más bajo. **`n` solo existe si el documento tiene tramo base** (columna `N` / `Normal` / `Base` / `-100` / `<100` / `1k`, es decir desde 0 kg): si la tabla empieza en `+45` o `+100`, `n` queda vacío y no se inventa. Excepción por perfil: EgyptAir (`n_from_lowest`) rellena `n` con el tramo más bajo, como sus Excel históricos.
   - **Tramos según el encabezado** (`tier_headers.py`): si todos los encabezados del documento coinciden se usan sus `from_kg`, aunque el modelo haya corrido las columnas (con `Min +45 … +1000` el modelo a veces devolvía `0, 45, 100, 300, 500` y todas las tarifas caían un tramo más abajo). Solo se corrige si la fila trae tantos tramos como columnas.
   - **Completitud:** se cuentan las líneas de datos del texto; si el modelo devuelve menos filas se reintenta (hasta 3 veces, con una nota que dice cuántas hay) y las respuestas incompletas no se guardan en cache. Si sigue incompleto el borrador trae `row_check: {expected, got, complete: false}` y el office lo avisa. Un archivo con más de ~320 líneas de datos se rechaza pidiendo elegir menos hojas (la respuesta del modelo se cortaría).
6. Se marcan aeropuertos que no existen en `transports` (`unknown`).

Reglas del prompt: *Via/Routing/Service* no son origen ni destino; un origen que es una ciudad con
aeropuertos (`NYC (JFK / EWR)`) genera una fila por aeropuerto; encabezados `45k`/`100k` son kilos.

## Insert

- `tariffs` (`type_tariff = 3`, `active = 1`) por fila, con `min + min_adjust`.
- `tariff_feeds` (USD/kg, `units = 1`): `header.fees` en todas las filas, `rows[].fees`
  solo en esa fila y el fuel de la fila como `fuel_fee_id` (FSC = 13) si es > 0.
- Todo o nada (rollback ante cualquier error).

## Tabla `airtariff_client_terms`

Una fila por `company_id`: `company_name`, `instructions`, `terms_files` (JSON
`[{name, chars, text, uploaded_at}]`), `updated_by`, timestamps. La crea el backend
(migración + `database/sql/cloud_sql_migration_2026_10_02_create_airtariff_client_terms.sql`).

## Fees por aerolínea (`air_fee_rules`)

Los recargos de una aerolínea (ej. EgyptAir: Transit Shipment Fee $0.05/kg, min $25, no aplica
a destino CAI) no se sacan del PDF ni del prompt: son reglas guardadas una vez por aerolínea en
`air_fee_rules` (backend: `AirFeeRuleController`, `AirFeeRules`). El office las muestra en
General fees y el backend las aplica al cotizar, con su mínimo y condiciones. **No se copian a
`tariff_feeds`** ni el mínimo se escribe en `fee_comment`: el insert solo recibe los fees simples
por kg (general / PDF / fila). Un fee con mínimo se carga como regla.

Las reglas se pueden proponer con IA: "✦ Suggest from … terms" en el office llama a
`POST /tariffs/rules/draft`, que manda las instrucciones y archivos guardados de la aerolínea
(y de su provider) + el catálogo de fees al LLM **una vez** (cacheado por hash del texto) y
devuelve reglas validadas (fee del catálogo, siglas que existen como aeropuerto). El usuario
revisa y guarda cada una; lo que no es una regla (cargos incluidos, condiciones que no son de
ruta, restricciones) vuelve en `skipped` con el motivo. Los cargos condicionados al envío
("if tendered unscreened", collect, perecederos, DG…) se descartan también por código, aunque el
modelo los proponga. Modelo: `TARIFF_RULES_MODEL` (`gpt-4.1-mini`; probado contra `gpt-4o-mini`,
que elige peor el fee del catálogo).

Por eso los términos guardados del cliente ya no fuerzan el LLM para EgyptAir: el parser
determinista se usa siempre que no haya instrucciones extra para esa subida.

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
| `TARIFF_RULES_MODEL` | `gpt-4.1-mini` | `gpt-4.1-mini` (propuesta de reglas de fees, `fee_rules_ai.py`) |

## Correr y desplegar

- Local: `uvicorn main:app --reload --host 0.0.0.0 --port 8001`.
- Cloud Run: `gcloud run deploy airtariffs --source . --region us-east1` (conserva env y
  secretos). Servicio: `https://airtariffs-717497920821.us-east1.run.app`.
- **Ojo:** en Cloud Run escribe en la BD de producción.
