"""Lectura DETERMINISTA de tablas de tarifas en Excel (sin LLM, sin tope de filas).

Muchos libros traen una tabla grande con un encabezado `Min | N | 45 | 100 | 300 | 500 | 1000` y una columna
de origen y otra de destino. Pasar ~1000 filas por el LLM no cabe (ni en el prompt ni en la respuesta) y es
innecesario: la estructura está en el encabezado. Aquí se detecta la tabla, se leen las filas tal cual y se
expande el origen cuando es una ZONA ("Eastern 1" = lista de aeropuertos que el libro define arriba de la
tabla). El LLM solo se usa luego para los metadatos (aerolínea, vigencia, comentarios).

Además de leer, describe la tabla para que el office deje elegir QUÉ filas cargar: cada columna de texto con
pocos valores distintos (Producto, Servicio, Región destino…) es un "facet" y se puede filtrar por sus
valores (`row_filter`: {columna: [valores]}), con cualquier combinación y por cualquier columna, no solo
por hojas. Una tabla que no se reconoce devuelve None y el archivo sigue el camino del LLM.
"""
import re
from dataclasses import dataclass, field

import tier_headers

IATA = re.compile(r"^[A-Z]{3}$")
CODES_IN_PARENS = re.compile(r"\(([A-Z]{3})\)")
NAME_WITH_CODE = re.compile(r"([A-Za-z][A-Za-z .'\-]*?)\s*\(([A-Z]{3})\)")
SEP = re.compile(r"[\s/,;|]+")
# Columnas de texto con más valores distintos que esto no son un filtro útil (comentarios, notas…).
MAX_FACET_VALUES = 150

_DEST_CODE_HEADER = re.compile(r"dest\w*\W*(code|iata|id)|^(to|dst|dest|destination)\W*(code|iata)?$|^arr", re.I)
_DEST_HEADER = re.compile(r"dest|^to$|^arr", re.I)
_ORIGIN_HEADER = re.compile(r"orig|^from$|^pol$|^dep|^station$", re.I)
_FUEL_HEADER = re.compile(r"fuel|fsc", re.I)


def _s(v) -> str:
    """Celda como texto de una línea ('' si vacía)."""
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.10g}"
    return " ".join(str(v).split())


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace("$", "").replace(",", "")
    try:
        return float(t) if t else None
    except ValueError:
        return None


@dataclass
class Table:
    sheet: str
    rows: list[list]
    header: int
    columns: list[str]
    min_col: int
    tier_cols: list[int]
    tiers: list[int]
    fuel_col: int | None
    origin_col: int
    dest_col: int
    data: list[int]  # índices (en rows) de las filas con destino y alguna tarifa
    no_rates: int  # filas con destino pero sin ninguna tarifa por tramo (p. ej. solo pivotes)
    zones: dict[str, list[str]] = field(default_factory=dict)
    # "miami" -> "MIA": ciudades que el libro nombra con su código entre paréntesis en las zonas
    names: dict[str, str] = field(default_factory=dict)
    facet_cols: list[int] = field(default_factory=list)

    def cell(self, i: int, c: int) -> str:
        row = self.rows[i]
        return _s(row[c]) if c < len(row) else ""

    def origins(self, raw: str) -> list[str]:
        """Aeropuertos de la celda de origen: zona definida en el libro, código, o lista dentro de la celda."""
        text = raw.strip()
        if not text:
            return []
        if text.lower() in self.zones:
            return self.zones[text.lower()]
        if IATA.match(text):
            return [text]
        if text.lower() in self.names:
            return [self.names[text.lower()]]
        codes = CODES_IN_PARENS.findall(text)
        if not codes:
            tokens = [t for t in SEP.split(text) if t]
            if tokens and all(IATA.match(t) for t in tokens):
                codes = tokens
        # sin duplicados, en orden
        return list(dict.fromkeys(codes)) or [text]


def _find_header(rows: list[list]) -> tuple[int, int, list[int]] | None:
    """(fila, columna de Min, tramos) del primer encabezado `Min` seguido de >=2 tramos de peso."""
    for i, row in enumerate(rows):
        cells = [_s(c) for c in row]
        for j, c in enumerate(cells):
            if not tier_headers._MIN.match(c.lower() if c else ""):
                continue
            tiers: list[int] = []
            for nxt in cells[j + 1 :]:
                v = tier_headers._tier(nxt) if nxt else None
                if v is None:
                    break
                tiers.append(v)
            if len(tiers) >= 2 and tiers == sorted(tiers):
                return i, j, tiers
    return None


def _zones(rows: list[list], upto: int) -> dict[str, list[str]]:
    """Zonas definidas arriba de la tabla: `Eastern 1 | Atlanta (ATL) / Chicago (ORD) / …`."""
    zones: dict[str, list[str]] = {}
    for row in rows[:upto]:
        cells = [_s(c) for c in row if _s(c)]
        if len(cells) < 2:
            continue
        label, rest = cells[0], " ".join(cells[1:])
        codes = CODES_IN_PARENS.findall(rest)
        if not codes:
            tokens = [t for t in SEP.split(rest) if t]
            codes = tokens if tokens and all(IATA.match(t) for t in tokens) else []
        if codes:
            zones[label.lower()] = list(dict.fromkeys(codes))
    return zones


def find_table(ws, name: str | None = None) -> Table | None:
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    found = _find_header(rows)
    if not found:
        return None
    h, min_col, tiers = found
    width = max((len(r) for r in rows), default=0)
    columns = [(_s(rows[h][c]) if c < len(rows[h]) else "") or f"Column {c + 1}" for c in range(width)]
    tier_cols = list(range(min_col + 1, min_col + 1 + len(tiers)))
    body = range(h + 1, len(rows))

    def col_values(c: int) -> list[str]:
        return [_s(rows[i][c]) for i in body if c < len(rows[i]) and _s(rows[i][c])]

    def iata_share(c: int) -> float:
        vals = col_values(c)
        return sum(1 for v in vals if IATA.match(v)) / len(vals) if vals else 0.0

    dest = next((c for c in range(width) if _DEST_CODE_HEADER.search(columns[c]) and iata_share(c) >= 0.8), None)
    if dest is None:
        dest = next((c for c in range(width) if _DEST_HEADER.search(columns[c]) and iata_share(c) >= 0.8), None)
    if dest is None:
        return None
    origin = next((c for c in range(width) if c != dest and _ORIGIN_HEADER.search(columns[c])), None)
    if origin is None:
        return None
    fuel = next((c for c in range(width) if _FUEL_HEADER.search(columns[c]) and c not in tier_cols), None)

    t = Table(name or ws.title, rows, h, columns, min_col, tier_cols, tiers, fuel, origin, dest, [], 0)
    t.zones = _zones(rows, h)
    for r in rows[:h]:
        for city, code in NAME_WITH_CODE.findall(" ".join(_s(c) for c in r if _s(c))):
            t.names.setdefault(city.strip().lower(), code)
    for i in body:
        if not IATA.match(t.cell(i, dest)):
            continue
        has_rate = any(_num(rows[i][c]) is not None for c in [min_col, *tier_cols] if c < len(rows[i]))
        if has_rate:
            t.data.append(i)
        else:
            t.no_rates += 1
    if not t.data:
        return None

    skip = {min_col, *tier_cols, *([fuel] if fuel is not None else [])}
    for c in range(width):
        if c in skip:
            continue
        vals = [t.cell(i, c) for i in t.data if t.cell(i, c)]
        if not vals:
            continue
        distinct = set(vals)
        numeric = sum(1 for v in vals if _num(v) is not None) / len(vals)
        if numeric > 0.5 or not 2 <= len(distinct) <= MAX_FACET_VALUES:
            continue
        t.facet_cols.append(c)
    return t


def describe(t: Table, with_matrix: bool = True) -> dict:
    """Resumen para el office: columnas filtrables con sus valores y, por fila, a qué valor pertenece.

    `matrix[k] = [índice de valor por cada facet…, tarifas que genera la fila]` (índice -1 = vacío). Con eso el
    office calcula al instante cuántas filas y tarifas dejaría cada combinación de filtros, sin volver a
    llamar al servicio.
    """
    facets = []
    index: list[dict[str, int]] = []
    for c in t.facet_cols:
        order: dict[str, int] = {}
        counts: dict[str, int] = {}
        for i in t.data:
            v = t.cell(i, c)
            if not v:
                continue
            order.setdefault(v, len(order))
            counts[v] = counts.get(v, 0) + 1
        index.append(order)
        facets.append({"column": t.columns[c], "values": [{"value": v, "count": counts[v]} for v in order]})
    matrix = []
    tariffs = 0
    for i in t.data:
        n = len(t.origins(t.cell(i, t.origin_col))) or 1
        tariffs += n
        if with_matrix:
            matrix.append([index[k].get(t.cell(i, c), -1) for k, c in enumerate(t.facet_cols)] + [n])
    return {
        "header_row": t.header + 1,
        "origin_column": t.columns[t.origin_col],
        "destination_column": t.columns[t.dest_col],
        "tiers": t.tiers,
        "lines": len(t.data),
        "tariffs": tariffs,
        "no_rates": t.no_rates,
        "zones": {k: len(v) for k, v in t.zones.items()},
        "facets": facets,
        **({"matrix": matrix} if with_matrix else {}),
    }


def _wanted_rows(t: Table, row_filter: dict[str, list[str]] | None) -> list[int]:
    if not row_filter:
        return t.data
    by_name = {c.casefold().strip(): i for i, c in enumerate(t.columns)}
    checks: list[tuple[int, set[str]]] = []
    for name, values in row_filter.items():
        c = by_name.get(name.casefold().strip())
        if c is None:
            raise ValueError(f"The column “{name}” is not in the sheet “{t.sheet}”.")
        if values:
            checks.append((c, {_s(v) for v in values}))
    return [i for i in t.data if all(t.cell(i, c) in allowed for c, allowed in checks)]


def extract(t: Table, row_filter: dict[str, list[str]] | None = None, label_column: str | None = None) -> list[dict]:
    """Filas del borrador (mismo formato que devuelve el LLM), una por aeropuerto de origen.

    `label_column`: columna cuyo valor se conserva en cada fila como `note` (el nombre del producto del
    archivo, p. ej. "AC DGR - Standard"): se guarda en los comentarios de cada tarifa aunque varios
    productos compartan commodity.
    """
    label_idx = None
    if label_column:
        label_idx = next((i for i, c in enumerate(t.columns) if c.casefold().strip() == label_column.casefold().strip()), None)
        if label_idx is None:
            raise ValueError(f"The column “{label_column}” is not in the sheet “{t.sheet}”.")
    out: list[dict] = []
    for i in _wanted_rows(t, row_filter):
        row = t.rows[i]
        get = lambda c: _num(row[c]) if c < len(row) else None  # noqa: E731
        breaks = [{"from_kg": tier, "rate": get(c)} for tier, c in zip(t.tiers, t.tier_cols) if get(c) is not None]
        mn = get(t.min_col)
        fuel = get(t.fuel_col) if t.fuel_col is not None else None
        note = t.cell(i, label_idx) if label_idx is not None else ""
        for origin in t.origins(t.cell(i, t.origin_col)):
            out.append({
                "origin": origin,
                "destination": t.cell(i, t.dest_col),
                "min": mn,
                "fuel_per_kg": fuel,
                "breaks": [dict(b) for b in breaks],
                **({"note": note} if note else {}),
            })
    return out


def meta_text(t: Table, sample: int = 8) -> str:
    """Texto corto para el LLM de metadatos: lo de arriba de la tabla, el encabezado y unas filas."""
    lines = []
    for r in t.rows[: t.header]:
        cells = [_s(c) for c in r if _s(c)]
        if cells:
            lines.append(" | ".join(cells))
    lines.append(" | ".join(c for c in t.columns))
    for i in t.data[:sample]:
        lines.append(" | ".join(_s(c) for c in t.rows[i] if _s(c)))
    return f"## Sheet: {t.sheet}\n" + "\n".join(lines)
