"""Tramos de peso según el ENCABEZADO de la tabla (no según lo que "interprete" el modelo).

El LLM a veces inventa un tramo base [0] o corre las columnas una posición (en un Excel de Emirates con
`Min | +45 | +100 | +300 | +500 | +1000` devolvió 0, 45, 100, 300, 500 en vez de 45…1000, y TODAS las
tarifas quedaron en el tramo equivocado). Como el encabezado está en el texto, se lee con reglas fijas y se
usa para fijar el `from_kg` de cada tramo. Solo se corrige cuando todos los encabezados del documento
coinciden y la fila trae tantos tramos como columnas; si no, se deja lo del modelo.
"""
import re

_SPLIT = re.compile(r"\s*\|\s*|\s+")

# Unidades y palabras de relleno que acompañan a los encabezados: "Minimum USD", "+100 USD/kg", "100 kg+", "Min charge".
_UNITS = re.compile(
    r"(?i)(?:us\$|usd|eur|gbp|cad|mxn|[$€£]|per\s*kgs?|/\s*kgs?|\bkgs?\b|(?<=\d)kgs?|\blbs?\b|\brates?\b|\bcharges?\b|\bcargo\b"
    r"|\btarifa\b|\bflat\b|\bper\s*shipment\b|\(|\)|\*)"
)
# Solo variantes inequívocas de "mínimo": nada de "M"/"MC" sueltas, que chocan con cualquier columna.
_MIN = re.compile(r"^(min(imum|imu|\.)?|m[ií]n(imo|\.)?)$", re.I)


def _clean(token: str) -> str:
    """Encabezado sin unidades ni relleno, en minúsculas: "+100 USD/kg" -> "+100", "Minimum USD" -> "minimum"."""
    return " ".join(_UNITS.sub(" ", token.lower().replace("\n", " ")).split())


def is_min(token: str) -> bool:
    """¿Es el encabezado de la tarifa mínima? (Min, Minimum, Minimum USD, Min. charge, Mínimo)."""
    return bool(token) and bool(_MIN.match(_clean(token)))


def _tier(token: str) -> int | None:
    """`from_kg` de un encabezado de tramo; None si no es un tramo."""
    t = _clean(token).replace(" ", "")
    if t in ("n", "normal", "base"):
        return 0
    m = re.fullmatch(r"(?:\+|>=?|≥|over|above|from|desde|q)(\d{1,5})k?\+?", t)  # "+100", ">100", "Q100"
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(\d{1,5})k?\+", t)  # "100+", "45k+"
    if m:
        return int(m.group(1))
    if re.fullmatch(r"(?:-|<=?|≤|under|below|hasta)(\d{1,5})k?", t):  # "-100", "<100": desde 0 kg
        return 0
    m = re.fullmatch(r"(\d{1,5})k?[-–—](\d{1,5})k?", t)  # rango "45-99", "100-299": desde el primero
    if m:  # el rango tiene que ser creciente
        return int(m.group(1)) if int(m.group(2)) > int(m.group(1)) else None
    m = re.fullmatch(r"(\d{1,5})k", t)  # "1k" = tramo base; "45k" = 45 kg
    if m:
        return 0 if int(m.group(1)) == 1 else int(m.group(1))
    return int(t) if re.fullmatch(r"\d{2,5}", t) else None  # "45", "100"


def detect(text: str) -> list[int] | None:
    """Tramos del encabezado (`from_kg` en orden de columna) si todos los encabezados del texto coinciden."""
    found: set[tuple[int, ...]] = set()
    for line in text.splitlines():
        line = re.sub(r"(?i)\bminimu\s+m\b", "minimum", line)  # celdas partidas: "Minimu | m"
        tokens = [t for t in _SPLIT.split(line.strip()) if t]
        for i, tok in enumerate(tokens):
            if not is_min(tok):
                continue
            tiers: list[int] = []
            for nxt in tokens[i + 1 :]:
                if not _clean(nxt):  # solo unidad ("USD/kg", "kg"): no corta la serie de tramos
                    continue
                v = _tier(nxt)
                if v is None:
                    break
                tiers.append(v)
            if len(tiers) >= 2 and all(a < b for a, b in zip(tiers, tiers[1:])):
                found.add(tuple(tiers))
            break  # una cabecera por línea
    return list(next(iter(found))) if len(found) == 1 else None


def align(rows: list[dict], tiers: list[int]) -> tuple[int, int]:
    """Fija el `from_kg` de cada tramo con el del encabezado. Devuelve (filas corregidas, filas que no cuadran)."""
    fixed = skipped = 0
    for r in rows:
        br = r.get("breaks") or []
        if len(br) != len(tiers):
            skipped += 1
            continue
        if any(b.get("from_kg") != t for b, t in zip(br, tiers)):
            fixed += 1
        for b, t in zip(br, tiers):
            b["from_kg"] = t
    return fixed, skipped


_NUM = re.compile(r"^\$?\d+(?:\.\d+)?$")
_IATA = re.compile(r"^[A-Z]{3}$")


def expected_rows(text: str) -> int | None:
    """Líneas de datos de la tabla según el propio texto (None si no se reconoce el encabezado de tramos).

    Una línea de datos tiene un código de 3 letras (aeropuerto) y tantos números como el mínimo más las
    columnas de tarifa del encabezado. Sirve para saber si el modelo devolvió TODAS las filas y si una
    hoja de un Excel es una tabla de tarifas.
    """
    tiers = detect(text)
    if not tiers:
        return None
    need = len(tiers) + 1
    count = 0
    for line in text.splitlines():
        tokens = [t for t in _SPLIT.split(line.strip()) if t]
        nums = sum(1 for t in tokens if _NUM.match(t))
        if nums >= need and any(_IATA.match(t) for t in tokens):
            count += 1
    return count or None
