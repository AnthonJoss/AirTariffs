"""PDF o Excel de tarifas aéreas -> filas normalizadas (borrador para revisión).

1. pdfplumber extrae el texto del PDF (o openpyxl el de las hojas del Excel).
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
import excel_reader
import tariff_db
import tier_headers
import versions_ai

CACHE_DIR = Path(__file__).parent / "cache"
SUPPORTED_SUFFIXES = (".pdf",) + excel_reader.EXCEL_SUFFIXES
MODEL = os.getenv("TARIFF_LLM_MODEL", "gpt-4o")

SYSTEM = """Extraes tarifas aéreas de carga desde el texto de un PDF enviado por una aerolínea o agente.
Responde SOLO con un JSON válido y COMPACTO (sin espacios ni saltos de línea innecesarios) con este esquema:
{"airline":"nombre o null","agent":"nombre o null","valid_from":"YYYY-MM-DD o null","valid_to":"YYYY-MM-DD o null",
 "comments":"condiciones relevantes en texto plano, breve",
 "rows":[["ORG","DST",min,fuel,[[desde_kg,tarifa],[desde_kg,tarifa]]]]}
Cada fila de "rows" es un arreglo: [origen IATA, destino IATA, min, fuel_por_kg, tramos].
Reglas:
- Una fila por par origen-destino. Si una línea trae varios destinos con la misma tarifa (ej. "BOM CGK $1.85 ...") o varios orígenes ("ATL/CLT" o "ATL CLT +100 ..." en el encabezado), expande en filas separadas.
- Encabezados de tramo con "k" ("1k 45k 100k 300k 500k 1000k") son KILOS (1, 45, 100, 300, 500, 1000 kg), no miles: "45k" -> [45,tarifa]. "1k" o "N" es el tramo base [0,tarifa].
- tramos: cada tramo de peso con su tarifa por kg. "+100" -> [100,tarifa]. "-100", "<100", "N" o "Normal" -> [0,tarifa].
- Cada columna de tarifa del encabezado es UN tramo con SU propio from_kg, tal cual está impreso: `Min +45 +100 +300 +500 +1000` -> [45,..],[100,..],[300,..],[500,..],[1000,..]. NUNCA inventes un tramo base [0] ni corras las columnas: el tramo base [0] existe SOLO si hay una columna explícita N / Normal / Base / "-X" / "<X" / "1k". Devuelve tantos tramos como columnas de tarifa tenga el encabezado.
- min es el cargo mínimo por envío (null si el PDF no lo trae). No lo mezcles con los tramos.
- fuel es la columna "Fuel x KG" de esa fila (0 si es 0, null si el PDF no tiene esa columna). NO la sumes a las tarifas. Ignora la columna "Rate All in".
- "origin" es donde sale la carga y "destination" a donde llega. Usa el contexto: "Nonstop uplift from JFK/BOS..." significa que JFK, BOS... son orígenes y la columna/encabezado "To" (FCO, MXP) es el destino; "Station: MIA" en una hoja de tarifas significa origen MIA.
- Columnas "Via" / "Routing" / "Service": indican la conexión o el tipo de servicio; NO son el origen ni el destino y no se descartan filas por traerlas. El destino de cada fila es su código IATA (columna "Code"/"Destination"); el origen sale del encabezado ("Tariff for NYC ...", "From ..."). Devuelve TODAS las filas de la tabla, también las que van vía otro aeropuerto.
- Si el origen es una ciudad con aeropuertos entre paréntesis ("NYC (JFK / EWR)", "JFK or EWR"), la tarifa vale para cada uno: genera una fila por aeropuerto listado y por destino (también cuando una fila diga "Direct EWR": el precio es el mismo). El total es (filas de la tabla × aeropuertos de origen): ninguna fila queda con un solo origen. Nunca devuelvas el código de ciudad (NYC).
- Reporta las tarifas exactamente como están impresas. No inventes datos: usa null si falta. Fechas como 01OCT26 -> 2026-10-01. Si solo se indica un mes ("Promo October 2026"), valid_from es el día 1 y valid_to el último día de ese mes."""


META_SYSTEM = """Lees el encabezado de una hoja de tarifas aéreas de carga (lo de arriba de la tabla, los nombres de columna y unas filas de ejemplo) y devuelves SOLO los datos generales, como un JSON válido y COMPACTO:
{"airline":"nombre o null","agent":"nombre o null","valid_from":"YYYY-MM-DD o null","valid_to":"YYYY-MM-DD o null","comments":"condiciones relevantes en texto plano, breve, o null"}
- La vigencia puede venir en el nombre del archivo ("July to Oct 2026" -> valid_from 2026-07-01, valid_to 2026-10-31) o en el texto. Si solo se indica un mes, valid_from es el día 1 y valid_to el último día.
- No inventes datos: usa null si falta. NO devuelvas filas de tarifas."""


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


def source_text(path: Path, sheets: list[str] | None = None) -> tuple[str, list[str] | None]:
    """(texto, hojas usadas); hojas usadas solo aplica a Excel."""
    if excel_reader.is_excel(path.name):
        text, used = excel_reader.excel_text(path, sheets)
        if not text:
            raise RuntimeError("El Excel no tiene datos en las hojas elegidas.")
        return text, used
    text = pdf_text(path)
    if not text:
        raise RuntimeError("El PDF no tiene texto extraíble (¿escaneado?). Requiere OCR.")
    return text, None


MAX_ATTEMPTS = 3
# Un archivo con muchas más líneas que esto no cabe en la respuesta del modelo (se cortaría el JSON).
MAX_EXPECTED_ROWS = 320


def _call_llm(system: str, text: str, nudge: str = "") -> dict:
    _load_env()
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Falta OPENAI_API_KEY (variable de entorno o archivo .env).")
    resp = OpenAI().chat.completions.create(
        model=MODEL,
        temperature=0,
        seed=7,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text + nudge},
        ],
    )
    data = json.loads(resp.choices[0].message.content)
    data["rows"] = expand_rows(data.get("rows", []))
    return data


def meta_with_llm(text: str, file_name: str = "", context: str = "") -> dict:
    """Aerolínea, agente, vigencia y comentarios de una tabla ya leída (las filas NO pasan por el modelo)."""
    _load_env()
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Falta OPENAI_API_KEY (variable de entorno o archivo .env).")
    prompt = f"File name: {file_name}\n\n{text[:8000]}"
    system = META_SYSTEM + context
    key = hashlib.sha256("\n".join([MODEL, "meta", system, prompt]).encode()).hexdigest()
    cache_file = CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    resp = OpenAI().chat.completions.create(
        model=MODEL,
        temperature=0,
        seed=7,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    )
    data = json.loads(resp.choices[0].message.content)
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data


def extract_with_llm(text: str, skip_comments: bool = False, context: str = "") -> dict:
    # context: términos/instrucciones del cliente (client_terms.prompt_section).
    system = SYSTEM + (NO_COMMENTS if skip_comments and not context else "") + context
    # Misma entrada + mismas instrucciones + mismo modelo = misma respuesta: se reutiliza del disco,
    # pero solo si estaba COMPLETA (una respuesta cortada no se guarda ni se reutiliza).
    key = hashlib.sha256("\n".join([MODEL, system, text]).encode()).hexdigest()
    cache_file = CACHE_DIR / f"{key}.json"
    expected = tier_headers.expected_rows(text)
    if expected and expected > MAX_EXPECTED_ROWS:
        raise ValueError(
            f"The file has about {expected} rows, more than can be read at once (max {MAX_EXPECTED_ROWS}). "
            "Pick fewer sheets (Sheets n/N) or split the file."
        )
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if expected is None or len(cached.get("rows", [])) >= expected:
            return cached

    best: dict | None = None
    nudge = ""
    for _ in range(MAX_ATTEMPTS):
        data = _call_llm(system, text, nudge)
        if best is None or len(data["rows"]) > len(best["rows"]):
            best = data
        if expected is None or len(data["rows"]) >= expected:
            CACHE_DIR.mkdir(exist_ok=True)
            cache_file.write_text(json.dumps(data), encoding="utf-8")
            return data
        # El modelo suele detenerse tras las filas con ruta explícita: se le dice cuántas hay.
        nudge = (
            f"\n\n[NOTA: el texto tiene {expected} líneas de datos. Devuelve UNA fila por cada línea "
            "(y una por cada aeropuerto de origen cuando el origen sea una ciudad con varios). "
            f"Tu respuesta anterior traía solo {len(data['rows'])} filas: faltaban.]"
        )
    assert best is not None
    best["row_check"] = {"expected": expected, "got": len(best["rows"]), "complete": False}
    return best


def pick_rate(breaks: list[dict], required: int):
    """Tarifa del tramo más alto <= required; si no hay, la del tramo más bajo."""
    if not breaks:
        return None
    le = [b for b in breaks if b["from_kg"] <= required]
    chosen = max(le, key=lambda b: b["from_kg"]) if le else min(breaks, key=lambda b: b["from_kg"])
    return chosen["rate"]


def to_columns(row: dict, fill_n: bool = False) -> dict:
    """Mapea tramos de peso a las columnas de `tariffs` (n, 45, 100, 300, 500, 1000).

    `n` es la tarifa del tramo base (desde 0 kg: columna N / Normal / "-100"…). Si el documento no tiene
    tramo base, `n` queda vacío: no se inventa. Solo con `fill_n` (perfil de cliente que lo pide, como
    EgyptAir) se rellena con la tarifa del tramo más bajo.
    """
    br = [b for b in row.get("breaks", []) if b.get("rate") is not None and b.get("from_kg") is not None]
    base = [b for b in br if b["from_kg"] == 0]
    if base:
        n_rate = base[0]["rate"]
    else:
        n_rate = min(br, key=lambda b: b["from_kg"])["rate"] if (br and fill_n) else None
    return {
        **({"note": row["note"]} if row.get("note") else {}),
        "origin": (row.get("origin") or "").upper(),
        "destination": (row.get("destination") or "").upper(),
        "min": row.get("min"),
        "fuel": row.get("fuel_per_kg"),
        "n": n_rate,
        "w45": pick_rate(br, 45),
        "w100": pick_rate(br, 100),
        "w300": pick_rate(br, 300),
        "w500": pick_rate(br, 500),
        "w1000": pick_rate(br, 1000),
    }


def comments_html(text) -> str:
    lines = [html.escape(l.strip().lstrip("•-* ").strip()) for l in (text or "").splitlines() if l.strip()]
    return "<p>" + "<br>".join(f"•{l}" for l in lines) + "</p>" if lines else ""


def parse_file(path: Path, client_id: int | None = None, instructions: str | None = None,
               sheets: list[str] | None = None, row_filter: dict[str, list[str]] | None = None,
               label_column: str | None = None) -> dict:
    """Borrador de tarifas desde un PDF o un Excel (.xlsx/.xlsm); mismo flujo para ambos.

    sheets: solo Excel; hojas a considerar (None = las visibles con datos).
    row_filter: solo Excel con tabla reconocida; {columna: [valores]} para cargar solo esas filas.
    label_column: solo Excel con tabla; columna cuyo valor (nombre del producto) se guarda en los comentarios de cada tarifa.

    Un Excel con una tabla reconocida (encabezado de tramos + columnas de origen y destino) se lee SIN el
    modelo, fila por fila y sin tope (excel_table); el modelo solo lee los metadatos (aerolínea, vigencia).
    """
    table = excel_reader.table_read(path, sheets, row_filter, label_column) if excel_reader.is_excel(path.name) else None
    if row_filter and not table:
        raise ValueError("The row filters need a sheet with a recognized rate table.")
    if table:
        used = table["used"]
        text = table["meta_text"]
        other_text = excel_reader.excel_text(path, table["other"])[0] if table["other"] else ""
    else:
        text, used = source_text(path, sheets)
        other_text = ""
    prof = client_profiles.detect(path.name, text)
    # Términos guardados del cliente elegido en el office o, si no eligió, del detectado
    # (provider y aerolínea del perfil) + instrucciones de esta subida.
    company_ids = [client_id] if client_id else ([prof["provider_id"], prof["airline_id"]] if prof else [])
    terms = tariff_db.get_client_terms(company_ids)
    # Versiones por commodity ("también súbelo como Dangerous con mínimo 100 y +0.50/kg"): las pide
    # el cliente en sus términos, en las instrucciones o el propio documento. Se detectan primero y su
    # frase se quita de lo que lee el LLM de las filas. Es un extra: si falla no se pierde el análisis.
    try:
        versions, versions_error = versions_ai.detect(terms, instructions, text[:4000]), None
    except Exception as e:  # noqa: BLE001
        versions, versions_error = [], f"{type(e).__name__}: {e}"
    read_terms, read_instructions = versions_ai.strip_sources(terms, instructions, versions)
    context = client_terms.prompt_section(read_terms, read_instructions)
    # EgyptAir tiene formato fijo: parser determinista (instantáneo, sin LLM); si no cuadra, cae al
    # LLM. Los términos guardados no lo fuerzan: sus fees viven en air_fee_rules (los aplica el
    # office/backend sin IA). Solo instrucciones extra de esta subida pasan por el LLM.
    use_fixed = prof and prof["name"] == "EgyptAir" and not (read_instructions or "").strip()
    # El parser fijo lee las líneas del PDF; con un Excel no cuadra y cae al LLM.
    data = egypt_parser.parse_egypt(text) if use_fixed and not table else None
    use_fixed_used = data is not None
    if table:
        # Filas leídas directamente de la tabla; el modelo solo aporta aerolínea / vigencia / comentarios.
        data = meta_with_llm(text, path.name, context)
        data["rows"] = table["rows"]
        data["reader"] = {"mode": "table", "lines": table["lines"], "tariffs": len(table["rows"]),
                          "filtered": bool(row_filter)}
        if other_text:
            extra = extract_with_llm(other_text, skip_comments=True, context=context)
            tiers = tier_headers.detect(other_text)
            if tiers:
                tier_headers.align(extra.get("rows", []), tiers)
            data["rows"] += extra.get("rows", [])
    elif data is None:
        data = extract_with_llm(text, skip_comments=bool(prof and prof.get("comments")), context=context)
    # Tramos según el encabezado de la tabla: el modelo a veces inventa un tramo base o corre las columnas.
    tiers = tier_headers.detect(text)
    if tiers and not use_fixed_used and not table:
        fixed, skipped = tier_headers.align(data.get("rows", []), tiers)
        data["tier_header"] = {"from_kg": tiers, "realigned_rows": fixed, "unmatched_rows": skipped}
    fill_n = bool(prof and prof.get("n_from_lowest"))
    data["rows"] = [to_columns(r, fill_n) for r in data.get("rows", [])]
    # Una columna de fuel toda en 0/None no es una columna real (EgyptAir): no se muestra ni crea fees.
    if not any(r["fuel"] for r in data["rows"]):
        for r in data["rows"]:
            r["fuel"] = None
    data["comments"] = comments_html(data.get("comments"))
    data["profile"] = prof
    if used is not None:
        data["excel"] = {"sheets": excel_reader.sheets_info(path, with_matrix=False), "used": used}
    data["ai_context"] = client_terms.summary(terms, instructions)
    data["versions"] = versions
    if versions_error:
        data["versions_error"] = versions_error
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
