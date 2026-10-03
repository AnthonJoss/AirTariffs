"""Términos de una aerolínea -> reglas de fees propuestas (tabla air_fee_rules, la del backend).

Una llamada al LLM por texto de términos (cacheada por hash, como tariff_parser): lee las
instrucciones y archivos guardados en airtariff_client_terms y devuelve las reglas con el mismo
esquema que guarda el office (fee del catálogo, base, monto, mínimo, tope y condiciones por
sigla IATA). Nada se guarda acá: el office las muestra como propuestas y el usuario confirma
cada una (el backend vuelve a validarlas al guardar).
"""
import hashlib
import json
import os
import re

import tariff_parser

# Modelo propio (no el del análisis de PDFs): extraer reglas de un texto corto alcanza con un "mini".
MODEL = os.getenv("TARIFF_RULES_MODEL", "gpt-4.1-mini")

BASES = ("per_kg", "per_kg_gross", "per_awb", "pct_freight")
CODE_KEYS = ("dest_in", "dest_not_in", "origin_in", "origin_not_in")
_IATA = re.compile(r"^[A-Z]{3}$")

SYSTEM = """Lees los términos/condiciones de tarifas aéreas de carga de una aerolínea y extraes los
RECARGOS (fees) que se cobran APARTE de la tarifa, como reglas estructuradas.
Responde SOLO con un JSON válido:
{"rules":[{"fee_id":id del catálogo o null,"label":"nombre corto del cargo","basis":"per_kg|per_kg_gross|per_awb|pct_freight",
  "amount":número,"min":número o null,"max":número o null,
  "dest_in":[IATA],"dest_not_in":[IATA],"origin_in":[IATA],"origin_not_in":[IATA],"aircraft":"PAX"|"CAO"|null,
  "valid_from":"YYYY-MM-DD"|null,"valid_to":"YYYY-MM-DD"|null,"source_text":"la frase original de donde sale"}],
 "skipped":[{"text":"frase","reason":"why it is not a rule, short, in English"}]}
Reglas:
- basis: "per_kg" = USD por kg cobrable (lo normal: "$.05/kg"); "per_kg_gross" solo si dice peso bruto/gross;
  "per_awb" = monto fijo por guía/AWB/MAWB/envío; "pct_freight" = porcentaje del flete (amount = el %, ej. 5).
- min/max: mínimo/tope en USD de ese cargo ("with a $25.00 min" -> min 25). null si no lo dice.
- Condiciones por aeropuerto con códigos IATA de 3 letras: "not applicable for final destination CAI" -> dest_not_in ["CAI"];
  "only from MIA" -> origin_in ["MIA"]. Listas vacías si no hay condición. aircraft solo si lo limita a PAX o CAO (freighter).
- valid_from/valid_to: solo si el cargo tiene fechas propias ("Peak Season from Nov 15 to Dec 31 2026"); si no dice el año,
  null. null si aplica siempre.
- fee_id: el id del catálogo que corresponda al cargo por su NOMBRE (ver CATÁLOGO). Prefiere los marcados [AIR] (usados en
  tarifas aéreas); nunca los marítimos/terrestres (DTHC, BL, AMS, ISF…). Equivalencias: transit/transfer/transshipment -> Transfer; security/ISPS ->
  Security Fee; screening -> Screening; fuel/FSC -> Fuel Surcharge; peak season -> Peak Season Surcharge (107); handling -> Handling.
  Si ningún [AIR] corresponde por nombre (ej. AWB fee, EDI), fee_id null: el usuario lo elige.
  label: el nombre del cargo como lo escribe la aerolínea (ej. "Transit Shipment Fee").
- Una regla se cobra SIEMPRE en esa ruta. Por eso NO son reglas (van a "skipped" con el motivo):
  * cargos ya incluidos en la tarifa ("rates are ALL-IN including fuel") y cargos sin monto ("Excluding EDI charges");
  * cargos condicionados a algo del envío que no es origen/destino/avión: "if tendered unscreened", "for charges collect"/CC,
    "AWB issued at counter", "fresh/perishable only", DG, special cargo, por tipo de mercancía;
  * restricciones y notas: peso mínimo, sobredimensiones, known shipper, validez, capacidad.
- No inventes montos ni aeropuertos. Usa "." como separador decimal ("$.05" = 0.05)."""


def _catalog_text(catalog: list[dict], air_ids: set[int]) -> str:
    return "\n".join(
        f"{f['id']}|{f.get('code') or ''}|{f.get('name') or ''}{' [AIR]' if f['id'] in air_ids else ''}" for f in catalog
    )


def ask_llm(terms_text: str, catalog: list[dict], air_ids: set[int], model: str | None = None) -> dict:
    """Llamada al LLM (o su respuesta cacheada) con los términos y el catálogo de fees."""
    model = model or MODEL
    system = SYSTEM + "\n\nCATÁLOGO (id|código|nombre):\n" + _catalog_text(catalog, air_ids)
    key = hashlib.sha256("\n".join(["fee_rules", model, system, terms_text]).encode()).hexdigest()
    cache_file = tariff_parser.CACHE_DIR / f"rules_{key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    tariff_parser._load_env()
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Falta OPENAI_API_KEY (variable de entorno o archivo .env).")
    from openai import OpenAI

    resp = OpenAI().chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": terms_text}],
    )
    data = json.loads(resp.choices[0].message.content)
    tariff_parser.CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n else None  # NaN


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Cargos que dependen de algo del envío que no es la ruta: una regla se cobra siempre en su ruta,
# así que no pueden serlo. Los modelos "mini" a veces los dejan pasar si la condición va en la misma
# frase ("Screening $0.10/kg if tendered unscreened"): se filtran acá, sin depender del modelo.
_SHIPMENT_CONDITION = re.compile(
    r"unscreened|if tendered|\bcollect\b|\bCC\b|at (?:the )?counter|perishable|\bfresh\b|\bDG\b|"
    r"dangerous goods|special cargo|hazardous|live animals|\bAVI\b",
    re.I,
)


def normalize(raw: dict, fee_ids: set[int], airports: set[str], air_ids: set[int] | None = None) -> dict:
    """Valida la respuesta del LLM contra el catálogo y los aeropuertos; cada regla lleva sus avisos."""
    rules, skipped = [], [s for s in (raw.get("skipped") or []) if isinstance(s, dict)]
    for r in raw.get("rules") or []:
        if not isinstance(r, dict):
            continue
        amount = _num(r.get("amount"))
        label = str(r.get("label") or "").strip()[:120]
        if not amount or amount <= 0 or not label:
            skipped.append({"text": r.get("source_text") or label, "reason": "No amount or no name."})
            continue
        source = str(r.get("source_text") or "").strip()[:500] or None
        cond = _SHIPMENT_CONDITION.search(f"{source or ''} {label}")
        if cond:
            skipped.append({
                "text": source or label,
                "reason": f"Depends on the shipment ('{cond.group(0)}'), not on the route: not an airline rule.",
            })
            continue
        warnings = []
        fee_id = r.get("fee_id")
        fee_id = int(fee_id) if isinstance(fee_id, (int, float, str)) and str(fee_id).isdigit() else None
        if fee_id not in fee_ids:
            fee_id = None
            warnings.append("Pick the fee of the catalog.")
        elif air_ids and fee_id not in air_ids:
            # Existe pero nunca se usó en una tarifa aérea (ej. DTHC, marítimo): que el usuario lo confirme.
            warnings.append("This fee was never used in air tariffs: check it.")
        basis = r.get("basis") if r.get("basis") in BASES else "per_kg"
        conditions = {}
        for k in CODE_KEYS:
            codes = [str(c).strip().upper() for c in (r.get(k) or []) if isinstance(c, str)]
            codes = list(dict.fromkeys(c for c in codes if _IATA.match(c)))
            bad = [c for c in codes if c not in airports]
            if bad:
                warnings.append(f"Not airports in the database: {', '.join(bad)} (removed).")
            if good := [c for c in codes if c in airports]:
                conditions[k] = good
        if r.get("aircraft") in ("PAX", "CAO"):
            conditions["aircraft"] = r["aircraft"]
        mn, mx = _num(r.get("min")), _num(r.get("max"))
        rules.append({
            "fee_id": fee_id,
            "label": label,
            "basis": basis,
            "amount": amount,
            "min": mn if mn and mn > 0 else None,
            "max": mx if mx and mx > 0 else None,
            "conditions": conditions,
            "valid_from": r["valid_from"] if _DATE.match(str(r.get("valid_from") or "")) else None,
            "valid_to": r["valid_to"] if _DATE.match(str(r.get("valid_to") or "")) else None,
            "source_text": source,
            "warnings": warnings,
        })
    return {"rules": rules, "skipped": skipped}


def terms_text(terms: list[dict]) -> str:
    """Instrucciones + texto de los archivos de términos guardados, uno tras otro."""
    parts = []
    for t in terms:
        if (t.get("instructions") or "").strip():
            parts.append(f"### Instrucciones ({t.get('company_name') or t['company_id']})\n{t['instructions'].strip()}")
        for f in t.get("files") or []:
            if (f.get("text") or "").strip():
                parts.append(f"### Archivo de términos: {f['name']}\n{f['text'].strip()}")
    body = "\n\n".join(parts)
    return body[:40_000]
