"""Términos/instrucciones por cliente para el análisis con IA (tabla airtariff_client_terms).

Cada cliente (provider o aerolínea, companies.id) puede tener instrucciones escritas
por el usuario y archivos de términos que manda la empresa (PDF/TXT). Se guarda el
texto ya extraído y, al analizar un PDF de ese cliente, se agrega al prompt del LLM
(tariff_parser.extract_with_llm) para que capture sus especificaciones.
"""
import io
from datetime import datetime, timezone

import pdfplumber

import excel_reader

ALLOWED = (".pdf", ".txt", ".md", ".csv") + excel_reader.EXCEL_SUFFIXES

# Topes para no reventar el contexto del modelo (el PDF de tarifas también va).
MAX_FILE_CHARS = 40_000
MAX_PROMPT_CHARS = 40_000


def extract_text(filename: str, content: bytes, sheets: list[str] | None = None) -> tuple[str, list[str] | None]:
    """(texto, hojas usadas); las hojas solo aplican a Excel (None = visibles con datos)."""
    name = filename.lower()
    used = None
    if excel_reader.is_excel(name):
        text, used = excel_reader.excel_text(content, sheets, excel_reader.EXCEL_MAX_CHARS)
        if not text:
            raise ValueError(f"{filename}: el Excel no tiene datos en las hojas elegidas.")
    elif name.endswith(".pdf"):
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            text = "\n\n".join((p.extract_text() or "") for p in pdf.pages).strip()
        if not text:
            raise ValueError(f"{filename}: el PDF no tiene texto extraíble (¿escaneado?).")
    elif name.endswith(ALLOWED):
        text = content.decode("utf-8", errors="replace").strip()
    else:
        raise ValueError(f"{filename}: solo se aceptan {', '.join(ALLOWED)}.")
    return text[:MAX_FILE_CHARS], used


def file_entry(filename: str, content: bytes, sheets: list[str] | None = None) -> dict:
    text, used = extract_text(filename, content, sheets)
    entry = {
        "name": filename,
        "chars": len(text),
        "text": text,
        "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    if used is not None:
        entry["sheets"] = used
    return entry


def prompt_section(terms: list[dict], extra: str | None = None) -> str:
    """Sección que se agrega al system prompt; "" si no hay nada que agregar."""
    parts = []
    for t in terms:
        if (t.get("instructions") or "").strip():
            parts.append(f"### Instrucciones para {t.get('company_name') or 'este cliente'}\n{t['instructions'].strip()}")
        for f in t.get("files") or []:
            if (f.get("text") or "").strip():
                parts.append(f"### Términos del cliente: {f['name']}\n{f['text'].strip()}")
    if (extra or "").strip():
        parts.append(f"### Instrucciones del usuario para este PDF\n{extra.strip()}")
    if not parts:
        return ""

    body = "\n\n".join(parts)
    if len(body) > MAX_PROMPT_CHARS:
        body = body[:MAX_PROMPT_CHARS] + "\n[… términos recortados por longitud]"
    return (
        "\n\nINSTRUCCIONES Y TÉRMINOS DEL CLIENTE\n"
        "Aplícalos al leer este PDF: cómo interpretar columnas, mínimos, recargos, exclusiones, "
        "orígenes/destinos y vigencias. Si las instrucciones del usuario contradicen las reglas "
        "generales de arriba, prevalecen las del usuario. Nunca inventes filas ni tarifas que no "
        "estén en el PDF. Las instrucciones de subir el archivo TAMBIÉN con otro commodity (Dangerous Goods, "
        "Perishables…, con otro mínimo o un aumento por kg) se aplican aparte: ignóralas al leer las filas y devuelve "
        "SIEMPRE todas las filas de la tabla con sus tarifas tal como están impresas. No sumes recargos a las tarifas: los fees (por kg, mínimos, exclusiones "
        "por destino) se aplican aparte con las reglas por aerolínea (air_fee_rules). Resume en "
        "\"comments\" las condiciones de estos términos que afecten la cotización (recargos, "
        "mínimos, exclusiones, restricciones de carga).\n\n" + body
    )


def summary(terms: list[dict], extra: str | None = None) -> dict | None:
    """Lo que ve el office en la tarjeta: qué contexto se usó en el análisis."""
    if not terms and not (extra or "").strip():
        return None
    return {
        "clients": [t.get("company_name") or f"#{t['company_id']}" for t in terms],
        "instructions": any((t.get("instructions") or "").strip() for t in terms),
        "files": [f["name"] for t in terms for f in (t.get("files") or [])],
        "extra": bool((extra or "").strip()),
    }
