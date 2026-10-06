"""Lectura de Excel (.xlsx/.xlsm) a texto para el LLM: tarifas y términos de cliente.

Una fila por línea, celdas separadas por " | ". Se leen los valores calculados (data_only).
Las filas y columnas ocultas por un filtro u ocultas a mano SÍ se leen: el filtro de Excel es una
vista, los datos están en el archivo. Las filas/columnas vacías se omiten.
"""
import io
import os
from datetime import date, datetime
from pathlib import Path

import openpyxl

EXCEL_SUFFIXES = (".xlsx", ".xlsm")
# Tope de texto por archivo (~15k tokens). Los libros de trabajo traen hojas auxiliares enormes
# (fórmulas que arman INSERTs, copias de la tarifa).
EXCEL_MAX_CHARS = int(os.getenv("TARIFF_EXCEL_MAX_CHARS", "60000"))


def is_excel(name: str) -> bool:
    return name.lower().endswith(EXCEL_SUFFIXES)


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return f"{v:.10g}"
    # Los encabezados traen saltos de línea ("Minimu\nm", "Fuel x\nKG"): una celda, una línea.
    return " ".join(str(v).split())


def _open(source):
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    elif isinstance(source, Path):
        source = str(source)
    return openpyxl.load_workbook(source, data_only=True)


def _lines(ws) -> tuple[list[str], int]:
    """Líneas de texto de la hoja y cuántas filas con datos estaban ocultas (por filtro u otro motivo)."""
    rows, hidden = [], 0
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        cells = [_cell(c) for c in row]
        if any(cells) and ws.row_dimensions[i].hidden:
            hidden += 1
        rows.append(cells)
    width = max((len(r) for r in rows), default=0)
    keep = [c for c in range(width) if any(c < len(r) and r[c] for r in rows)]
    lines = [" | ".join((r[c] if c < len(r) else "") for c in keep).strip(" |") for r in rows]
    return [l for l in lines if l], hidden


def sheets_info(source) -> list[dict]:
    """Hojas del libro para que el usuario elija: filas con datos, ocultas por filtro y selección por defecto."""
    wb = _open(source)
    out = []
    for ws in wb.worksheets:
        lines, hidden = _lines(ws)
        visible = ws.sheet_state == "visible"
        out.append({
            "name": ws.title,
            "visible": visible,
            "rows": len(lines),
            "hidden_rows": hidden,
            "chars": sum(len(l) + 1 for l in lines),
            "selected": visible and bool(lines),
        })
    wb.close()
    return out


def excel_text(source, sheets: list[str] | None = None, max_chars: int = EXCEL_MAX_CHARS) -> tuple[str, list[str]]:
    """(texto, hojas usadas).

    sheets=None: hojas visibles con datos, en orden, mientras quepan en max_chars (las demás se omiten).
    sheets=[...]: exactamente esas (también hojas ocultas); si alguna no cabe es un error, no se omite en silencio.
    """
    explicit = sheets is not None
    if explicit and not sheets:
        raise ValueError("Select at least one sheet.")
    wb = _open(source)
    try:
        by_name = {ws.title: ws for ws in wb.worksheets}
        missing = [s for s in (sheets or []) if s not in by_name]
        if missing:
            raise ValueError(f"Sheet(s) not found in the file: {', '.join(missing)}.")
        wanted = [by_name[s] for s in sheets] if explicit else [w for w in wb.worksheets if w.sheet_state == "visible"]
        parts, used, skipped, size = [], [], [], 0
        for ws in wanted:
            lines, _ = _lines(ws)
            if not lines:
                continue
            block = f"## Sheet: {ws.title}\n" + "\n".join(lines)
            if size + len(block) > max_chars:
                skipped.append(ws.title)
                continue
            parts.append(block)
            used.append(ws.title)
            size += len(block)
    finally:
        wb.close()
    if skipped and (explicit or not parts):
        raise ValueError(f"The selected sheets are too long ({max_chars} characters max): {', '.join(skipped)}.")
    if skipped:
        parts.append(f"[Sheets omitted for length: {', '.join(skipped)}]")
    return "\n\n".join(parts).strip(), used
