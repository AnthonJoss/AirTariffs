"""Parser determinista para los PDFs de promo de EgyptAir (ATC Aviation).

Formato fijo: una línea de encabezado con 1+ orígenes y los tramos ("ATL CLT +100 +250 +500 +1000 +3000")
y una línea por destino(s) con los precios ("BOM CGK $1.85 $1.80 ..."). Devuelve None si algo no cuadra,
para que el llamador use el LLM como respaldo.
"""
import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

_IATA = re.compile(r"^[A-Z]{3}$")
_HEADER_TIER = re.compile(r"^\+(\d+)$")
_PRICE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")


def _parse_validity(text: str):
    # "Validity from 01OCT26 – 31OCT26", "Validity from 01OCT–31OCT26" (año solo al final)
    m = re.search(r"Valid\w*\s+(?:from\s+)?(\d{1,2})([A-Z]{3})(\d{2})?\s*\W{1,3}\s*(\d{1,2})([A-Z]{3})(\d{2})", text, re.I)
    if m:
        d1, m1, y1, d2, m2, y2 = m.groups()
        try:
            return (date(2000 + int(y1 or y2), MONTHS[m1.upper()], int(d1)),
                    date(2000 + int(y2), MONTHS[m2.upper()], int(d2)))
        except (KeyError, ValueError):
            return None
    # "from October 1st, 2026 until October 31st, 2026", "Valid October 1st, 2026 – October 31st 2026"
    m = re.search(
        r"Valid\w*\s+(?:from\s+)?([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s*(?:until|to|through|\W{1,3})\s*"
        r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text, re.I)
    if m:
        mo1, d1, y1, mo2, d2, y2 = m.groups()
        try:
            return (date(int(y1), MONTHS[mo1[:3].upper()], int(d1)),
                    date(int(y2), MONTHS[mo2[:3].upper()], int(d2)))
        except (KeyError, ValueError):
            return None
    return None


def parse_egypt(text: str) -> dict | None:
    validity = _parse_validity(text)
    if not validity:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    origins, tiers, start = None, None, None
    for i, ln in enumerate(lines):
        parts = [t for t in ln.replace("/", " ").split() if t.upper() != "CURRENCY"]
        codes = [p for p in parts if _IATA.match(p)]
        plus = [_HEADER_TIER.match(p) for p in parts if p.startswith("+")]
        if codes and len(plus) >= 2 and all(plus) and parts[: len(codes)] == codes and len(parts) == len(codes) + len(plus):
            origins, tiers, start = codes, [int(p.group(1)) for p in plus], i
            break
    if not origins:
        return None

    rows = []
    for ln in lines[start + 1:]:
        if "$" not in ln:
            if rows:
                break
            continue
        head, _, _ = ln.partition("$")
        dests = [t for t in head.replace("/", " ").split() if t.upper() != "USD"]
        prices = [float(x) for x in _PRICE.findall(ln)]
        if not dests or not all(_IATA.match(d) for d in dests) or len(prices) != len(tiers):
            return None
        for o in origins:
            for d in dests:
                rows.append({
                    "origin": o, "destination": d, "min": None, "fuel_per_kg": None,
                    "breaks": [{"from_kg": t, "rate": p} for t, p in zip(tiers, prices)],
                })
    if not rows:
        return None
    return {
        "airline": "Egypt Air", "agent": None,
        "valid_from": validity[0].isoformat(), "valid_to": validity[1].isoformat(),
        "comments": "", "rows": rows,
    }
