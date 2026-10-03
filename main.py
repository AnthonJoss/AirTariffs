import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import client_terms
import fee_rules_ai
import gmail_client
import tariff_db
import tariff_parser

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

def _draft(path: Path, client_id: int | None = None, instructions: str | None = None) -> dict:
    data = tariff_parser.parse_pdf(path, client_id, instructions)
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
):
    """Sube un PDF y devuelve el borrador extraído (NO inserta nada).

    client_id: empresa cuyos términos guardados se aplican (si no, los del perfil detectado).
    instructions: instrucciones extra solo para este análisis.
    """
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Solo PDF")
    UPLOAD_DIR.mkdir(exist_ok=True)
    dest = UPLOAD_DIR / Path(file.filename).name
    dest.write_bytes(await file.read())
    try:
        return await run_in_threadpool(_draft, dest, client_id, instructions)
    except Exception as e:
        raise HTTPException(422, f"{type(e).__name__}: {e}")


def _terms_view(company_id: int) -> dict:
    found = tariff_db.get_client_terms([company_id])
    t = found[0] if found else {"company_id": company_id, "company_name": None, "instructions": "", "files": [],
                                "updated_by": None, "updated_at": None}
    # El texto de los archivos no viaja al office (solo nombre y tamaño).
    return {**t, "files": [{k: f[k] for k in ("name", "chars", "uploaded_at")} for f in t["files"]]}


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
    updated_by: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    """Guarda los términos de una empresa: instrucciones + archivos nuevos (PDF/TXT) y los
    existentes que se conservan (keep: JSON con sus nombres)."""
    try:
        keep_names = set(json.loads(keep or "[]"))
        current = tariff_db.get_client_terms([company_id])
        kept = [f for f in (current[0]["files"] if current else []) if f["name"] in keep_names]
        new = [client_terms.file_entry(f.filename or "terms.txt", await f.read()) for f in files]
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
async def tariff_companies(q: str):
    return await run_in_threadpool(tariff_db.search_companies, q)


@app.get("/tariffs/fees")
async def tariff_fees():
    return await run_in_threadpool(tariff_db.fees_catalog)


@app.get("/tariffs/commodities")
async def tariff_commodities():
    return await run_in_threadpool(tariff_db.commodities)


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
