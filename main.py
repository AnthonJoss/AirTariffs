import hmac
import json
import os
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import client_terms
import excel_reader
import fee_rules_ai
import gmail_client
import sheet_links
import tariff_archive
import tariff_db
import tariff_parser
import upload_history

UPLOAD_DIR = Path(__file__).parent / "uploads"

app = FastAPI()

# office_tms (Vite en :5174, o su dominio en producción) llama a /tariffs/* desde el navegador.
# En Cloud Run CORS_ORIGIN_REGEX agrega el dominio de office-tms.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=os.getenv("CORS_ORIGIN_REGEX", r"https?://(localhost|127\.0\.0\.1)(:\d+)?"),
    allow_methods=["*"],
    allow_headers=["*"],
)

# En Cloud Run el servicio es público (como el resto): con TARIFF_API_KEY definida,
# todo salvo "/" exige el header X-Api-Key (lo manda tmsbackendnew). En local no se define.
API_KEY = os.getenv("TARIFF_API_KEY")


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if API_KEY and request.url.path != "/" and request.method != "OPTIONS":
        if not hmac.compare_digest(request.headers.get("x-api-key", ""), API_KEY):
            return JSONResponse({"detail": "API key inválida"}, status_code=401)
    return await call_next(request)


@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}


class DownloadRequest(BaseModel):
    ids: list[str]


@app.get("/mails")
async def list_mails():
    """Últimos 30 correos de franco@dataservicios.com."""
    try:
        return await run_in_threadpool(gmail_client.list_mails)
    except gmail_client.NotAuthorized as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/mails/download")
async def download_mails(body: DownloadRequest):
    """Descarga los PDFs de los correos seleccionados a la carpeta pdfs/."""
    result = {}
    try:
        for mail_id in body.ids:
            result[mail_id] = await run_in_threadpool(gmail_client.download_pdfs, mail_id)
    except gmail_client.NotAuthorized as e:
        raise HTTPException(status_code=401, detail=str(e))
    return result


@app.get("/mails-ui", response_class=HTMLResponse)
async def mails_ui():
    return PAGE


# ---------------- Tarifas: upload -> revisión -> insert ----------------

def _draft(path: Path, client_id: int | None = None, instructions: str | None = None,
           sheets: list[str] | None = None, row_filter: dict[str, list[str]] | None = None,
           label_column: str | None = None) -> dict:
    data = tariff_parser.parse_file(path, client_id, instructions, sheets, row_filter, label_column)
    ids = tariff_db.transport_ids([r["origin"] for r in data["rows"]] + [r["destination"] for r in data["rows"]])
    for r in data["rows"]:
        r["unknown"] = [c for c in (r["origin"], r["destination"]) if c not in ids]
    prof = data.get("profile")
    if prof:
        names = tariff_db.company_names([prof["provider_id"], prof["airline_id"]])
        prof["provider_name"] = names.get(prof["provider_id"])
        prof["airline_name"] = names.get(prof["airline_id"])
    return data


@app.post("/tariffs/parse")
async def parse_tariff_pdf(
    file: UploadFile = File(...),
    client_id: int | None = Form(None),
    instructions: str | None = Form(None),
    sheets: str | None = Form(None),
    row_filter: str | None = Form(None),
    label_column: str | None = Form(None),
):
    """Sube un PDF o Excel (.xlsx/.xlsm) y devuelve el borrador extraído (NO inserta nada).

    client_id: empresa cuyos términos guardados se aplican (si no, los del perfil detectado).
    instructions: instrucciones extra solo para este análisis.
    sheets: JSON con los nombres de las hojas a leer (solo Excel; vacío = las visibles con datos).
    row_filter: JSON {columna: [valores]} (solo Excel con tabla): carga únicamente las filas que cumplan.
    label_column: columna (solo Excel con tabla) cuyo valor se guarda como `note` en cada fila → comentarios de la tarifa.
    """
    if not (file.filename or "").lower().endswith(tariff_parser.SUPPORTED_SUFFIXES):
        raise HTTPException(400, "Solo PDF o Excel (.xlsx, .xlsm)")
    # Una carpeta por petición: varias tarjetas del MISMO archivo (una por hoja de un Excel) llegan a la vez
    # con el mismo nombre; si compartieran ruta una sobrescribiría el archivo mientras otra lo lee
    # (BadZipFile: Truncated file header). El nombre se conserva (la detección de cliente lo usa).
    request_dir = UPLOAD_DIR / uuid.uuid4().hex
    request_dir.mkdir(parents=True, exist_ok=True)
    dest = request_dir / Path(file.filename).name
    dest.write_bytes(await file.read())
    try:
        sheet_names = json.loads(sheets) if sheets else None
        filters = json.loads(row_filter) if row_filter else None
        if filters is not None and not (isinstance(filters, dict) and all(
                isinstance(v, list) for v in filters.values())):
            raise ValueError("row_filter must be a JSON object {column: [values]}.")
        return await run_in_threadpool(_draft, dest, client_id, instructions, sheet_names, filters, label_column or None)
    except Exception as e:
        raise HTTPException(422, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(request_dir, ignore_errors=True)


@app.post("/tariffs/excel-sheets")
async def excel_sheets(file: UploadFile = File(...)):
    """Hojas de un Excel (filas con datos, ocultas por filtro y selección por defecto) para elegir cuáles leer."""
    if not excel_reader.is_excel(file.filename or ""):
        raise HTTPException(400, "Solo Excel (.xlsx, .xlsm)")
    try:
        return {"sheets": await run_in_threadpool(excel_reader.sheets_info, await file.read())}
    except Exception as e:
        raise HTTPException(422, f"{type(e).__name__}: {e}")


def _terms_view(company_id: int) -> dict:
    found = tariff_db.get_client_terms([company_id])
    t = found[0] if found else {"company_id": company_id, "company_name": None, "instructions": "", "files": [],
                                "updated_by": None, "updated_at": None}
    # El texto de los archivos no viaja al office (solo nombre y tamaño).
    return {**t, "files": [{k: f[k] for k in ("name", "chars", "uploaded_at", "sheets") if k in f} for f in t["files"]]}


@app.get("/tariffs/terms")
async def get_terms(company_id: int):
    """Términos/instrucciones guardados de una empresa para el análisis con IA."""
    return await run_in_threadpool(_terms_view, company_id)


@app.post("/tariffs/terms")
async def save_terms(
    company_id: int = Form(...),
    company_name: str = Form(""),
    instructions: str = Form(""),
    keep: str = Form("[]"),
    sheets: str = Form("{}"),
    updated_by: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    """Guarda los términos de una empresa: instrucciones + archivos nuevos (PDF/TXT) y los
    existentes que se conservan (keep: JSON con sus nombres). sheets: JSON {archivo: [hojas]} para los
    Excel nuevos (sin entrada = las visibles con datos)."""
    try:
        keep_names = set(json.loads(keep or "[]"))
        current = tariff_db.get_client_terms([company_id])
        kept = [f for f in (current[0]["files"] if current else []) if f["name"] in keep_names]
        by_file = json.loads(sheets or "{}")
        new = [client_terms.file_entry(f.filename or "terms.txt", await f.read(), by_file.get(f.filename))
               for f in files]
        names = {f["name"] for f in new}
        merged = [f for f in kept if f["name"] not in names] + new
        await run_in_threadpool(
            tariff_db.save_client_terms, company_id, company_name, instructions.strip(), merged, updated_by
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return await run_in_threadpool(_terms_view, company_id)


class RulesDraftRequest(BaseModel):
    airline_id: int
    # Empresas cuyos términos guardados se leen (la aerolínea y, si hay, su provider).
    company_ids: list[int] = []
    # Texto extra solo para esta propuesta (ej. una línea pegada a mano).
    text: str = ""


def _rules_draft(body: RulesDraftRequest) -> dict:
    terms = tariff_db.get_client_terms([body.airline_id, *body.company_ids])
    text = "\n\n".join(p for p in (fee_rules_ai.terms_text(terms), body.text.strip()) if p)
    if not text:
        raise ValueError("No saved terms or instructions for this airline. Save them in AI instructions ▸ Client terms first.")
    catalog = tariff_db.fees_catalog()
    air_ids = tariff_db.air_fee_ids()
    raw = fee_rules_ai.ask_llm(text, catalog, air_ids)
    codes = [c for r in raw.get("rules") or [] if isinstance(r, dict)
             for k in fee_rules_ai.CODE_KEYS for c in (r.get(k) or []) if isinstance(c, str)]
    airports = set(tariff_db.transport_ids([c.strip().upper() for c in codes]))
    out = fee_rules_ai.normalize(raw, {f["id"] for f in catalog}, airports, air_ids)
    return {**out, "airline_id": body.airline_id, "sources": [t.get("company_name") or t["company_id"] for t in terms]}


@app.post("/tariffs/rules/draft")
async def rules_draft(body: RulesDraftRequest):
    """Propone reglas de fees (air_fee_rules) a partir de los términos guardados de la aerolínea.
    Una llamada al LLM por texto (cacheada); NO guarda nada: el office las confirma una por una."""
    try:
        return await run_in_threadpool(_rules_draft, body)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(422, str(e))


@app.get("/tariffs/companies")
async def tariff_companies(q: str, type: int | None = None):
    """`type` opcional: tipo de empresa (types.id), p. ej. 6 = Air Carrier para el campo Airline."""
    return await run_in_threadpool(tariff_db.search_companies, q, 15, type)


@app.get("/tariffs/airports")
async def tariff_airports(q: str):
    return await run_in_threadpool(tariff_db.search_airports, q)


@app.get("/tariffs/fees")
async def tariff_fees():
    return await run_in_threadpool(tariff_db.fees_catalog)


@app.get("/tariffs/commodities")
async def tariff_commodities():
    return await run_in_threadpool(tariff_db.commodities)


class NewCommodity(BaseModel):
    name: str


@app.post("/tariffs/commodities")
async def create_commodity(body: NewCommodity):
    """Crea un commodity de carga aérea (o devuelve el existente con ese nombre)."""
    try:
        return await run_in_threadpool(tariff_db.create_commodity, body.name)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


class InsertRequest(BaseModel):
    header: dict
    rows: list[dict]


@app.post("/tariffs/insert")
async def insert_tariffs(body: InsertRequest):
    """Inserta en MySQL (local) las filas confirmadas en la pantalla de revisión."""
    try:
        n = await run_in_threadpool(tariff_db.insert_tariffs, body.header, body.rows)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return n


# ---------------- Histórico de tarifas (archivar / reemplazar / restaurar) ----------------

def _archive_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except tariff_archive.ArchiveError as e:
        raise HTTPException(422, str(e))


class ReplacePreviewRequest(BaseModel):
    airline_id: int
    provider_id: int | None = None
    commodity_id: int | None = None
    # Commodity base + los de sus versiones ("también como DG…"): el conteo suma todos
    commodity_ids: list[int] = []
    airchaft: int | None = None
    # {mode: same_routes|whole_airline|airline_all|batches, batch_ids?, any_provider?}
    replace: dict
    # [[origen, destino], …] del archivo nuevo (siglas)
    pairs: list[list[str]] = []
    run_id: str | None = None
    batch_id: str | None = None


@app.post("/tariffs/replace/preview")
async def replace_preview(body: ReplacePreviewRequest):
    """Cuántas tarifas vigentes pasarían al histórico si se sube esto con "reemplazar lo anterior" (no toca nada)."""
    return await run_in_threadpool(_archive_call, tariff_archive.preview_replace, body.model_dump())


class ArchiveRequest(BaseModel):
    batch_ids: list[str] = []
    tariff_ids: list[int] = []
    archived_by: str | None = None
    dry_run: bool = False


@app.post("/tariffs/archive")
async def archive_tariffs(body: ArchiveRequest):
    """Pasa al histórico una tanda (lotes) o tarifas sueltas, sin subir nada nuevo."""
    return await run_in_threadpool(
        _archive_call, tariff_archive.archive_manual, body.batch_ids, body.tariff_ids, body.archived_by, body.dry_run
    )


@app.get("/tariffs/history")
async def tariffs_history(airline_id: int, limit: int = 2000):
    """Histórico de una aerolínea: grupos (qué subida las reemplazó) y tarifas."""
    return await run_in_threadpool(_archive_call, tariff_archive.history, airline_id, limit)


class RestoreRequest(BaseModel):
    archive_ids: list[int]
    restored_by: str | None = None


@app.post("/tariffs/history/restore")
async def restore_history(body: RestoreRequest):
    """Devuelve tarifas del histórico a vigentes."""
    return await run_in_threadpool(_archive_call, tariff_archive.restore, body.archive_ids, body.restored_by)


# ---------------- Links (Google Sheet publicado) ----------------

class LinkRequest(BaseModel):
    url: str
    name: str | None = None
    origin: str
    provider_id: int
    airline_id: int
    commodity_id: int
    valid_days: int = sheet_links.DEFAULT_VALID_DAYS
    created_by: str | None = None
    # Commodities especiales (DG…): [{commodity_id, pct, fee: {fee_id, amount, basis, label}}]
    versions: list[dict] = []


class LinkSyncRequest(BaseModel):
    # True: vuelve a cargar aunque la hoja no haya cambiado.
    force: bool = False
    synced_by: str | None = None


def _link_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except sheet_links.LinkNotFound:
        raise HTTPException(404, "That link is not registered.")
    except sheet_links.LinkError as e:
        raise HTTPException(422, str(e))


class LinkPreviewRequest(BaseModel):
    url: str


@app.post("/tariffs/links/preview")
async def preview_link(body: LinkPreviewRequest):
    """Process: lee la hoja y dice qué contiene (sin guardar nada) para precargar el formulario."""
    return await run_in_threadpool(_link_call, sheet_links.preview, body.url)


@app.get("/tariffs/links")
async def list_links():
    return await run_in_threadpool(sheet_links.list_links)


@app.post("/tariffs/links")
async def register_link(body: LinkRequest):
    """Registra el link y hace la primera lectura (inserta las tarifas)."""
    return await run_in_threadpool(_link_call, sheet_links.register, body.model_dump(), body.created_by)


@app.get("/tariffs/links/{link_id}/tariffs")
async def link_tariffs(link_id: int):
    """Las tarifas que hoy tiene el link (para seguirlas en la pestaña Links)."""
    return await run_in_threadpool(_link_call, sheet_links.link_tariffs, link_id)


@app.get("/tariffs/links/{link_id}/runs")
async def link_runs(link_id: int, limit: int = 30):
    """Historial de revisiones del link (actualizó / sin cambios / error)."""
    return await run_in_threadpool(_link_call, sheet_links.link_runs, link_id, limit)


@app.post("/tariffs/links/sync-all")
async def sync_all_links():
    """Revisa todos los links; solo actualiza los que cambiaron."""
    return await run_in_threadpool(sheet_links.sync_all)


@app.post("/tariffs/links/{link_id}/sync")
async def sync_link(link_id: int, body: LinkSyncRequest | None = None):
    """Sync now: baja la hoja y, si cambió (o `force`), reemplaza las tarifas del link."""
    body = body or LinkSyncRequest()
    return await run_in_threadpool(_link_call, sheet_links.sync, link_id, body.force, body.synced_by)


# ---------------- Historial de uploads y reversa ----------------

@app.get("/tariffs/uploads")
async def list_uploads(limit: int = 30):
    """Últimos uploads (un archivo = un lote, con sus versiones por commodity)."""
    return await run_in_threadpool(upload_history.list_uploads, max(1, min(limit, 100)))


class RevertRequest(BaseModel):
    batch_id: str
    reverted_by: str | None = None
    # True: solo cuenta qué pasaría (para mostrarlo antes de confirmar), sin tocar nada.
    dry_run: bool = False


@app.post("/tariffs/uploads/revert")
async def revert_upload(body: RevertRequest):
    """Revierte un upload: borra sus tarifas y fees; las ya usadas en cotizaciones las desactiva."""
    try:
        return await run_in_threadpool(upload_history.revert_batch, body.batch_id, body.reverted_by, body.dry_run)
    except upload_history.BatchNotFound:
        raise HTTPException(404, "That upload is not in the history.")
    except upload_history.AlreadyReverted:
        raise HTTPException(409, "That upload was already reverted.")
    except upload_history.TrackingUnavailable as e:
        raise HTTPException(503, str(e))


class EditRequest(BaseModel):
    batch_ids: list[str]
    # airline_id, provider_id, commodity_id, valid_to (YYYY-MM-DD), airchaft (1 CAO, 2 PAX, 3 ambos); vacío = no cambia
    changes: dict
    edited_by: str | None = None
    dry_run: bool = False


@app.post("/tariffs/uploads/edit")
async def edit_uploads(body: EditRequest):
    """Corrige aerolínea / proveedor / commodity / vigencia / avión de todas las tarifas de esos uploads."""
    try:
        return await run_in_threadpool(
            upload_history.edit_batches, body.batch_ids, body.changes, body.edited_by, body.dry_run
        )
    except upload_history.BatchNotFound as e:
        raise HTTPException(404, f"Upload not in the history: {e}")
    except upload_history.EditNotAllowed as e:
        raise HTTPException(422, str(e))
    except upload_history.TrackingUnavailable as e:
        raise HTTPException(503, str(e))


class AttachRulesRequest(BaseModel):
    batch_id: str
    rule_ids: list[int]


@app.post("/tariffs/uploads/rules")
async def attach_upload_rules(body: AttachRulesRequest):
    """Asocia a un upload las reglas de fees (air_fee_rules) que creó, para borrarlas si se revierte."""
    try:
        n = await run_in_threadpool(upload_history.attach_rules, body.batch_id, body.rule_ids)
    except upload_history.BatchNotFound:
        raise HTTPException(404, "That upload is not in the history.")
    except upload_history.TrackingUnavailable as e:
        raise HTTPException(503, str(e))
    return {"batch_id": body.batch_id, "rules": n}


@app.get("/tariffs-ui", response_class=HTMLResponse)
async def tariffs_ui():
    return (Path(__file__).parent / "tariffs_ui.html").read_text(encoding="utf-8")


PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Correos franco@dataservicios.com</title>
<style>
body{font-family:system-ui,sans-serif;margin:24px;max-width:980px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left}
button{padding:8px 16px;margin:8px 0}#msg{white-space:pre-wrap;margin-top:12px}
</style></head><body>
<h2>Últimos 30 correos de franco@dataservicios.com</h2>
<button id="dl" disabled>Descargar PDFs seleccionados</button>
<table><thead><tr><th></th><th>Fecha</th><th>Asunto</th><th>PDFs</th></tr></thead><tbody id="rows"></tbody></table>
<div id="msg">Cargando...</div>
<script>
const rows=document.getElementById('rows'),msg=document.getElementById('msg'),dl=document.getElementById('dl');
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
fetch('/mails').then(async r=>{const d=await r.json().catch(()=>({detail:'Error '+r.status}));if(!r.ok)throw d.detail;return d}).then(list=>{
  msg.textContent=list.length+' correos';
  rows.innerHTML=list.map(m=>`<tr><td><input type="checkbox" value="${m.id}" ${m.pdfs.length?'':'disabled'}></td>
  <td>${esc(m.date)}</td><td>${esc(m.subject)}</td><td>${m.pdfs.length?esc(m.pdfs.join(', ')):'—'}</td></tr>`).join('');
}).catch(e=>msg.textContent='Error: '+e);
rows.addEventListener('change',()=>dl.disabled=!rows.querySelector('input:checked'));
dl.onclick=async()=>{
  const ids=[...rows.querySelectorAll('input:checked')].map(i=>i.value);
  dl.disabled=true;msg.textContent='Descargando...';
  const r=await fetch('/mails/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});
  const d=await r.json().catch(()=>({detail:'Error '+r.status}));
  if(!r.ok){msg.textContent='Error: '+d.detail;dl.disabled=false;return}
  msg.textContent='Guardados en pdfs/:\\n'+Object.values(d).flat().join('\\n');
  dl.disabled=false;
};
</script></body></html>
"""
