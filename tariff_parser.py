"""PDF de tarifas aéreas -> filas normalizadas (borrador para revisión).

1. pdfplumber extrae el texto del PDF.
2. Un LLM lo convierte a un JSON con esquema fijo (cada cliente envía un formato distinto).
3. El código mapea los tramos de peso a las columnas de `tariffs` (n, 45, 100, 300, 500, 1000).
"""
import hashlib
import html
import json
import os
import re
from pathlib import Path

import pdfplumber
from openai import OpenAI

import client_profiles
import client_terms
import egypt_parser
import tariff_db

CACHE_DIR = Path(__file__).parent / "cache"
MODEL = os.getenv("TARIFF_LLM_MODEL", "gpt-4o")

SYSTEM = """Extraes tarifas aéreas de carga desde el texto de un PDF enviado por una aerolínea o agente.
Responde SOLO con un JSON válido y COMPACTO (sin espacios ni saltos de línea innecesarios) con este esquema:
{"airline":"nombre o null","agent":"nombre o null","valid_from":"YYYY-MM-DD o null","valid_to":"YYYY-MM-DD o null",
 "comments":"condiciones relevantes en texto plano, breve",
 "rows":[["ORG","DST",min,fuel,[[desde_kg,tarifa],[desde_kg,tarifa]]]]}
Cada fila de "rows" es un arreglo: [origen IATA, destino IATA, min, fuel_por_kg, tramos].
Reglas:
- Una fila por par origen-destino. Si una línea trae varios destinos con la misma tarifa (ej. "BOM CGK $1.85 ...") o varios orígenes ("ATL/CLT" o "ATL CLT +100 ..." en el encabezado), expande en filas separadas.
- tramos: cada tramo de peso con su tarifa por kg. "+100" -> [100,tarifa]. "-100", "<100", "N" o "Normal" -> [0,tarifa].
- min es el cargo mínimo por envío (null si el PDF no lo trae). No lo mezcles con los tramos.
- fuel es la columna "Fuel x KG" de esa fila (0 si es 0, null si el PDF no tiene esa columna). NO la sumes a las tarifas. Ignora la columna "Rate All in".
- "origin" es donde sale la carga y "destination" a donde llega. Usa el contexto: "Nonstop uplift from JFK/BOS..." significa que JFK, BOS... son orígenes y la columna/encabezado "To" (FCO, MXP) es el destino; "Station: MIA" en una hoja de tarifas significa origen MIA.
- Reporta las tarifas exactamente como están impresas. No inventes datos: usa null si falta. Fechas como 01OCT26 -> 2026-10-01. Si solo se indica un mes ("Promo October 2026"), valid_from es el día 1 y valid_to el último día de ese mes."""


NO_COMMENTS = '\nIMPORTANTE: devuelve "comments": null (no los necesito).'


def _load_env():
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def expand_rows(compact: list) -> list[dict]:
    rows = []
    for r in compact:
        origin, destination, min_charge, fuel, tiers = (list(r) + [None] * 5)[:5]
        rows.append({
            "origin": origin,
            "destination": destination,
            "min": min_charge,
            "fuel_per_kg": fuel,
            "breaks": [{"from_kg": t[0], "rate": t[1]} for t in (tiers or []) if len(t) == 2],
        })
    return rows


_BROKEN_AMOUNT = re.compile(r"\$\s*(\d(?:[\d ]*\d)?)\s*\.\s*(\d+)")


def pdf_text(path: Path) -> str:
    with pdfplumber.open(path) as pdf:
        text = "\n\n".join((p.extract_text() or "") for p in pdf.pages).strip()
    # Algunos PDFs parten los montos con espacios ("$ 1 20.00", "$ 7 .14"): se reconstruyen.
    return _BROKEN_AMOUNT.sub(lambda m: f"${m.group(1).replace(' ', '')}.{m.group(2)}", text)


def extract_with_llm(text: str, skip_comments: bool = False, context: str = "") -> dict:
    # context: términos/instrucciones del cliente (client_terms.prompt_section).
    system = SYSTEM + (NO_COMMENTS if skip_comments and not context else "") + context
    # Misma entrada + mismas instrucciones + mismo modelo = misma respuesta: se reutiliza del disco.
    key = hashlib.sha256("\n".join([MODEL, system, text]).encode()).hexdigest()
    cache_file = CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    _load_env()
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Falta OPENAI_API_KEY (variable de entorno o archivo .env).")
    resp = OpenAI().chat.completions.create(
        model=MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
    )
    data = json.loads(resp.choices[0].message.content)
    data["rows"] = expand_rows(data.get("rows", []))
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data


def pick_rate(breaks: list[dict], required: int):
    """Tarifa del tramo más alto <= required; si no hay, la del tramo más bajo."""
    if not breaks:
        return None
    le = [b for b in breaks if b["from_kg"] <= required]
    chosen = max(le, key=lambda b: b["from_kg"]) if le else min(breaks, key=lambda b: b["from_kg"])
    return chosen["rate"]


def to_columns(row: dict) -> dict:
    """Mapea tramos de peso a las columnas de `tariffs` (n, 45, 100, 300, 500, 1000)."""
    br = [b for b in row.get("breaks", []) if b.get("rate") is not None and b.get("from_kg") is not None]
    lowest = min(br, key=lambda b: b["from_kg"])["rate"] if br else None
    return {
        "origin": (row.get("origin") or "").upper(),
        "destination": (row.get("destination") or "").upper(),
        "min": row.get("min"),
        "fuel": row.get("fuel_per_kg"),
        "n": lowest,
        "w45": pick_rate(br, 45),
        "w100": pick_rate(br, 100),
        "w300": pick_rate(br, 300),
        "w500": pick_rate(br, 500),
        "w1000": pick_rate(br, 1000),
    }


def comments_html(text) -> str:
    lines = [html.escape(l.strip().lstrip("•-* ").strip()) for l in (text or "").splitlines() if l.strip()]
    return "<p>" + "<br>".join(f"•{l}" for l in lines) + "</p>" if lines else ""


def parse_pdf(path: Path, client_id: int | None = None, instructions: str | None = None) -> dict:
    text = pdf_text(path)
    if not text:
        raise RuntimeError("El PDF no tiene texto extraíble (¿escaneado?). Requiere OCR.")
    prof = client_profiles.detect(path.name, text)
    # Términos guardados del cliente elegido en el office o, si no eligió, del detectado
    # (provider y aerolínea del perfil) + instrucciones de esta subida.
    company_ids = [client_id] if client_id else ([prof["provider_id"], prof["airline_id"]] if prof else [])
    terms = tariff_db.get_client_terms(company_ids)
    context = client_terms.prompt_section(terms, instructions)
    # EgyptAir tiene formato fijo: parser determinista (instantáneo); si no cuadra, cae al LLM.
    # Con términos/instrucciones se usa el LLM, que es el que puede aplicarlos.
    data = egypt_parser.parse_egypt(text) if prof and prof["name"] == "EgyptAir" and not context else None
    if data is None:
        data = extract_with_llm(text, skip_comments=bool(prof and prof.get("comments")), context=context)
    data["rows"] = [to_columns(r) for r in data.get("rows", [])]
    # Una columna de fuel toda en 0/None no es una columna real (EgyptAir): no se muestra ni crea fees.
    if not any(r["fuel"] for r in data["rows"]):
        for r in data["rows"]:
            r["fuel"] = None
    data["comments"] = comments_html(data.get("comments"))
    data["profile"] = prof
    data["ai_context"] = client_terms.summary(terms, instructions)
    if prof and not context:
        data["comments"] = prof.get("comments", data["comments"])
    elif prof and prof.get("comments"):
        # Con términos, se conservan los comentarios fijos del perfil y se suman los del LLM.
        data["comments"] = prof["comments"] + data["comments"]
    if prof:
        if prof.get("min_rule") == "100kg_x_rate100":
            for r in data["rows"]:
                if r["min"] is None and r["w100"] is not None:
                    r["min"] = round(100 * r["w100"])
    return data
