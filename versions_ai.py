"""Términos / instrucciones -> versiones por commodity ("este PDF también se sube como Dangerous").

A veces los términos del cliente, las instrucciones escritas o el propio documento piden subir las
MISMAS tarifas otra vez con otro commodity (Dangerous Goods, Perishables…), con un mínimo propio y/o
un aumento por kg o un porcentaje. Una llamada al LLM, solo si el texto menciona algo parecido
(cacheada por hash). Nada se inserta acá: el office las muestra como "Also upload as another
commodity" ya llenas, con la etiqueta "from the terms", y el usuario las revisa antes de insertar.
"""
import hashlib
import json
import os
import re

MODEL = os.getenv("TARIFF_RULES_MODEL", "gpt-4.1-mini")
MODES = ("surcharge", "pct", "same")
MAX_CHARS = 40_000

# Evita la llamada al LLM si el texto no menciona ni un commodity especial ni "subir también".
_HINT = re.compile(
    r"dangerous|\bDGR?\b|hazmat|hazardous|perishable|pharma|live animal|valuable|"
    r"(also|too|as well)\b.{0,40}\b(upload|load|rate)|tambi[eé]n.{0,40}\b(sub|carg)|mismo (pdf|archivo)",
    re.I,
)

SYSTEM = """Decides si los términos/instrucciones del cliente o el inicio de un documento de tarifas aéreas piden
EXPLÍCITAMENTE subir las mismas tarifas OTRA VEZ con otro commodity (ej. Dangerous Goods, Perishables, Pharmaceuticals,
Live Animals), con un mínimo propio y/o un aumento.
Responde SOLO con un JSON válido:
{"versions":[{"commodity":"nombre del commodity como lo escribe el texto","mode":"surcharge|pct|same",
  "amount":número o null,"min":número o null,"source_text":"la frase original"}]}
- mode "surcharge": a cada tramo de peso se le suma amount USD por kg ("0.50 más en cada kg" -> amount 0.5).
  mode "pct": las tarifas son amount % de las generales ("150% of the general rate" -> amount 150).
  mode "same": mismas tarifas (solo cambia el commodity y/o el mínimo).
- min: mínimo propio de esa versión en USD ("con un mínimo de 100" -> 100). null si no lo dice.
- SOLO si el texto ordena subir/cargar las tarifas también con ese commodity, p. ej. "este PDF también se sube como
  Dangerous pero con un mínimo de 100 y 0.50 más en cada kg" o "also upload as DG with $0.30/kg more".
- NO es una instrucción de subida: una tabla de productos o recargos ("DGR contact sales", "XPS 200% of listed GCR",
  "UN fee $125"), una mención de mercancía peligrosa, ni restricciones. En esos casos devuelve {"versions":[]}.
- No inventes montos ni commodities. Usa "." como separador decimal."""


def _blob(terms: list[dict], extra: str | None, doc_head: str) -> str:
    parts = []
    for t in terms:
        if (t.get("instructions") or "").strip():
            parts.append(f"### Instrucciones escritas ({t.get('company_name') or t['company_id']})\n{t['instructions'].strip()}")
        for f in t.get("files") or []:
            if (f.get("text") or "").strip():
                parts.append(f"### Términos del cliente: {f['name']}\n{f['text'].strip()}")
    if (extra or "").strip():
        parts.append(f"### Instrucciones para este archivo\n{extra.strip()}")
    if doc_head.strip():
        parts.append(f"### Inicio del documento de tarifas\n{doc_head.strip()}")
    return "\n\n".join(parts)[:MAX_CHARS]


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def normalize(raw: dict) -> list[dict]:
    out, seen = [], set()
    for v in (raw or {}).get("versions") or []:
        name = str(v.get("commodity") or "").strip()
        mode = v.get("mode") if v.get("mode") in MODES else None
        amount, minimum = _num(v.get("amount")), _num(v.get("min"))
        if not name or not mode or name.lower() in seen:
            continue
        if mode in ("surcharge", "pct") and amount is None:
            continue  # aumento sin monto: no se puede aplicar
        seen.add(name.lower())
        out.append({
            "commodity": name,
            "mode": mode,
            "amount": amount if mode != "same" else None,
            "min": minimum,
            "source_text": str(v.get("source_text") or "")[:300] or None,
        })
    return out


def detect(terms: list[dict], extra: str | None, doc_head: str) -> list[dict]:
    """Versiones por commodity pedidas por los términos, las instrucciones o el documento ([] si no hay)."""
    blob = _blob(terms, extra, doc_head)
    if not _HINT.search(blob):
        return []
    key = hashlib.sha256("\n".join(["versions", MODEL, SYSTEM, blob]).encode()).hexdigest()

    import tariff_parser  # import perezoso: tariff_parser importa este módulo

    cache_file = tariff_parser.CACHE_DIR / f"versions_{key}.json"
    if cache_file.exists():
        return normalize(json.loads(cache_file.read_text(encoding="utf-8")))

    tariff_parser._load_env()
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Falta OPENAI_API_KEY (variable de entorno o archivo .env).")
    from openai import OpenAI

    resp = OpenAI().chat.completions.create(
        model=MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": blob}],
    )
    data = json.loads(resp.choices[0].message.content)
    tariff_parser.CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return normalize(data)


def strip_sources(terms: list[dict], extra: str | None, versions: list[dict]) -> tuple[list[dict], str | None]:
    """Quita de los términos y de las instrucciones la frase de cada versión detectada.

    Esas frases ("también súbelo como Dangerous con mínimo 100…") las atiende `detect`; si llegan al
    prompt que lee las filas del PDF lo confunden y devuelve filas de menos.
    """
    phrases = [re.compile(re.escape(v["source_text"]), re.I) for v in versions if v.get("source_text")]
    if not phrases:
        return terms, extra

    def clean(text: str) -> str:
        for rx in phrases:
            text = rx.sub("", text)
        return text

    cleaned = []
    for t in terms:
        c = dict(t)
        c["instructions"] = clean(t.get("instructions") or "")
        c["files"] = [{**f, "text": clean(f.get("text") or "")} for f in (t.get("files") or [])]
        cleaned.append(c)
    return cleaned, clean(extra or "") if extra else extra
