"""Tarifas spot desde un Google Sheet publicado ("Publicar en la web"): registro del link y sincronización.

Flujo (sin IA, sin procesos en segundo plano): el link se registra una vez (`airtariff_links`) y cada
`sync` descarga el CSV (~1 KB), compara su hash con el de la última lectura y SOLO si cambió reemplaza
las tarifas: inserta las nuevas y después revierte el lote anterior (así nunca hay un hueco sin tarifas).

Formato esperado de la hoja (WEEKLY SPOTS): filas `UPDATE DATE | mm/dd/yyyy`, `WK | n` y una tabla
`DEST | ENTRY CONDITION ALL IN/KG RANGE | A/C`. Destino = código IATA (también "GRU VIA MCO"), tarifa en
USD/kg con coma decimal o `CLOSED`, A/C = CAO (airchaft 1) o PAX (airchaft 2). Los spots aplican a +300 kg:
la tarifa va a los tramos +300/+500/+1000 y el mínimo es tarifa × 300.
"""
import csv
import hashlib
import io
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta

import mysql.connector

import tariff_db
import upload_history
from db_conn import db_conn

PROFILE = os.getenv("MYSQL_PROFILE", "local")
AIRCRAFT = {"CAO": 1, "PAX": 2}
MIN_KG = 300
DEFAULT_VALID_DAYS = 7
MAX_CSV_BYTES = 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS airtariff_links (
    id                INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name              VARCHAR(120) NOT NULL,
    url               VARCHAR(500) NOT NULL,
    origin_code       VARCHAR(8)   NOT NULL,
    provider_id       INT NOT NULL,
    airline_id        INT NOT NULL,
    commodity_id      INT NOT NULL,
    valid_days        INT NOT NULL DEFAULT 7,
    last_hash         VARCHAR(64)  NULL,
    last_update_date  DATE NULL,
    last_week         VARCHAR(12)  NULL,
    last_batch_ids    TEXT NULL,
    last_tariffs      INT UNSIGNED NULL,
    last_synced_at    TIMESTAMP NULL,
    last_status       VARCHAR(12)  NULL,
    last_message      VARCHAR(500) NULL,
    versions          TEXT NULL,
    created_by        VARCHAR(120) NULL,
    created_at        TIMESTAMP NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

RUNS_SCHEMA = """
CREATE TABLE airtariff_link_runs (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    link_id     INT UNSIGNED NOT NULL,
    status      VARCHAR(12)  NOT NULL,
    message     VARCHAR(500) NULL,
    tariffs     INT UNSIGNED NULL,
    week        VARCHAR(12)  NULL,
    update_date DATE NULL,
    run_by      VARCHAR(120) NULL,
    created_at  TIMESTAMP NULL,
    PRIMARY KEY (id),
    KEY airtariff_link_runs_link_index (link_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_table_ready = False


class LinkError(ValueError):
    """Link inválido, inaccesible o con un formato que no se reconoce."""


class LinkNotFound(LookupError):
    pass


def ensure_table() -> None:
    """Crea la tabla si falta (una vez por proceso; el CREATE hace commit implícito, va fuera de transacciones)."""
    global _table_ready
    if _table_ready:
        return
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'airtariff_links'"
        )
        if not cur.fetchone():
            cur.execute(SCHEMA)
        else:
            cur.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() "
                "AND table_name = 'airtariff_links' AND column_name = 'versions'"
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE airtariff_links ADD COLUMN versions TEXT NULL")
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'airtariff_link_runs'"
        )
        if not cur.fetchone():
            cur.execute(RUNS_SCHEMA)
        conn.commit()
    _table_ready = True


# ---------------- Descarga y lectura de la hoja ----------------

def csv_url(url: str) -> str:
    """Convierte el link de la hoja publicada (…/pubhtml) en su CSV (…/pub?output=csv), conservando el gid."""
    url = (url or "").strip()
    m = re.match(r"^https://docs\.google\.com/spreadsheets/(?:u/\d+/)?d/e/([\w-]+)/pub(?:html)?(?:[/?].*)?$", url)
    if not m:
        raise LinkError("The link must be a published Google Sheet (File ▸ Share ▸ Publish to web).")
    gid = re.search(r"[?&#]gid=(\d+)", url)
    return (
        f"https://docs.google.com/spreadsheets/d/e/{m.group(1)}/pub?output=csv" + (f"&gid={gid.group(1)}" if gid else "")
    )


def fetch_csv(url: str) -> str:
    req = urllib.request.Request(csv_url(url), headers={"User-Agent": "airtariffs-sync/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(MAX_CSV_BYTES + 1)
    except Exception as e:  # red, 404 si se despublicó, etc.
        raise LinkError(f"Could not download the sheet: {e}") from e
    if len(raw) > MAX_CSV_BYTES:
        raise LinkError("The sheet is too large to be a spot rates list.")
    return raw.decode("utf-8-sig", errors="replace")


def _rate(text: str) -> float | None:
    """'4,20' -> 4.2; 'CLOSED' o vacío -> None."""
    t = (text or "").strip().replace(" ", "")
    if not re.fullmatch(r"\d+(?:[.,]\d+)?", t):
        return None
    return float(t.replace(",", "."))


def parse_sheet(text: str) -> dict:
    """{update_date, week, notes, rows:[{dest, via, rate, ac}], closed:[…]} desde el CSV de la hoja."""
    update_date = week = origin = None
    notes: list[str] = []
    rows: list[dict] = []
    closed: list[str] = []
    in_table = False
    for line in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in line]
        # Los valores pueden estar en la primera columna o desplazados una a la derecha (hoja con margen).
        while cells and not cells[0]:
            cells = cells[1:]
        if not cells:
            continue
        key = cells[0].upper()
        if origin is None:
            # "ORIGIN: MIA", "ORIGEN MIA", "DEPARTURE - MIA" en cualquier celda de la cabecera.
            m_o = re.search(r"\b(?:ORIGIN|ORIGEN|DEPARTURE)\b\s*[:\-]?\s*([A-Z]{3})\b", " ".join(cells).upper())
            if m_o:
                origin = m_o.group(1)
        if key == "UPDATE DATE" and len(cells) > 1:
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    update_date = datetime.strptime(cells[1], fmt).date()
                    break
                except ValueError:
                    continue
        elif key == "WK" and len(cells) > 1:
            week = cells[1]
        elif key == "DEST":
            in_table = True
        elif in_table and len(cells) >= 3 and cells[2].upper() in AIRCRAFT:
            m = re.match(r"^([A-Za-z]{3})(?:\s+VIA\s+([A-Za-z]{3}))?$", cells[0])
            if not m:
                continue
            ac = cells[2].upper()
            rate = _rate(cells[1])
            if rate is None:
                closed.append(f"{m.group(1).upper()} {ac}")
            else:
                rows.append({"dest": m.group(1).upper(), "via": (m.group(2) or "").upper() or None, "rate": rate, "ac": ac})
        elif not in_table and len(cells[0]) > 40:
            notes.append(" ".join(cells[0].split()))  # condiciones generales (cabecera libre)
    if update_date is None:
        raise LinkError("The sheet has no 'UPDATE DATE'. Is this the spot rates sheet?")
    if not rows:
        raise LinkError("No rates were found in the sheet (expected the DEST / rate / A/C table).")
    text_notes = " ".join(notes).upper()
    commodity_hint = "general cargo" if "GENERAL CARGO" in text_notes else None
    specials = detect_specials(" ".join(notes))
    return {
        "update_date": update_date, "week": week, "notes": notes, "rows": rows, "closed": closed,
        "origin": origin, "commodity_hint": commodity_hint, "specials": specials,
    }


_DG_RE = re.compile(
    r"(?:DG|HAZMAT|DANGEROUS(?: GOODS)?)\b[^.]{0,40}?ADD[ -]?ON\s*(\d+(?:[.,]\d+)?)\s*%"
    r"(?:\s*\+\s*(\d+(?:[.,]\d+)?)\s*USD\s*/\s*([A-Z]+(?: TYPE)?))?",
    re.I,
)


def detect_specials(text: str) -> list[dict]:
    """Commodities especiales que declara la cabecera: "DG - HAZMAT ADD ON 25% + 100 USD/UN TYPE" -> Dangerous
    Goods con +25 % sobre las tarifas (y el mínimo) y un cargo fijo de 100 USD por tipo UN."""
    out = []
    m = _DG_RE.search(text or "")
    if m:
        num = lambda v: float(v.replace(",", ".")) if v else None  # noqa: E731
        out.append({
            "kind": "dangerous", "commodity_hint": "dangerous", "pct": num(m.group(1)),
            "flat": num(m.group(2)), "flat_unit": (m.group(3) or "").upper() or None,
            "text": " ".join(m.group(0).split()),
        })
    return out


def _comments(link: dict, parsed: dict) -> str:
    wk = f" WK {parsed['week']}" if parsed["week"] else ""
    text = f"Spot rate{wk} (updated {parsed['update_date']}). " + " ".join(parsed["notes"])
    return "<p>" + text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:900] + "</p>"


def preview(url: str) -> dict:
    """Qué contiene la hoja (no guarda nada): fecha, semana, destinos, tipos de avión y lo que se pudo deducir
    (origen, commodity) para precargar el formulario. El origen solo se propone si existe en `transports`."""
    parsed = parse_sheet(fetch_csv(url))
    dests = sorted({r["dest"] for r in parsed["rows"]})
    origin = parsed["origin"]
    ids = tariff_db.transport_ids([*dests, *([origin] if origin else [])])
    by_ac: dict[str, int] = {}
    for r in parsed["rows"]:
        by_ac[r["ac"]] = by_ac.get(r["ac"], 0) + 1
    return {
        "update_date": parsed["update_date"].isoformat(),
        "week": parsed["week"],
        "tariffs": len(parsed["rows"]),
        "by_aircraft": by_ac,
        "destinations": dests,
        "unknown_destinations": [d for d in dests if d not in ids],
        "closed": parsed["closed"],
        "notes": parsed["notes"],
        # Lo que se cargaría por fila (mismo cálculo que `sync`): tarifa/kg en +300/+500/+1000 y mínimo.
        "rows": [
            {
                "destination": r["dest"], "via": r["via"], "aircraft": r["ac"], "rate": r["rate"],
                "min": round(r["rate"] * MIN_KG), "known": r["dest"] in ids,
            }
            for r in parsed["rows"]
        ],
        "origin": origin if origin in ids else None,
        "commodity_hint": parsed["commodity_hint"],
        "specials": parsed["specials"],
    }


FEE_BASES = ("per_kg", "per_kg_gross", "per_awb", "pct_freight")


def _clean_versions(raw) -> list[dict]:
    """Versiones por commodity del link: [{commodity_id, pct, fee: {fee_id, amount, basis, label} | None}]."""
    out = []
    for v in raw or []:
        try:
            commodity_id, pct = int(v["commodity_id"]), float(v.get("pct") or 0)
        except (KeyError, TypeError, ValueError):
            raise LinkError("Each special commodity needs a commodity and a percentage.") from None
        if not 0 <= pct <= 500:
            raise LinkError("The percentage of a special commodity must be between 0 and 500.")
        fee = v.get("fee")
        if fee:
            try:
                fee = {
                    "fee_id": int(fee["fee_id"]), "amount": float(fee["amount"]),
                    "basis": fee.get("basis") if fee.get("basis") in FEE_BASES else "per_awb",
                    "label": " ".join(str(fee.get("label") or "").split())[:120] or "Special commodity fee",
                }
            except (KeyError, TypeError, ValueError):
                raise LinkError("The fee of a special commodity needs a fee and an amount.") from None
            if fee["amount"] <= 0:
                fee = None
        out.append({"commodity_id": commodity_id, "pct": pct, "fee": fee or None, "kind": v.get("kind")})
    return out


def _create_fee_rule(link: dict, version: dict, fee: dict, user: str | None) -> int:
    """Regla de fee (air_fee_rules) de la versión: solo para ese commodity, proveedor y origen. Se asocia al lote."""
    cond = {
        "commodity_in": [version["commodity_id"]], "provider_in": [link["provider_id"]], "origin_in": [link["origin"]],
    }
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO air_fee_rules (airline_id, fee_id, label, basis, amount, conditions, active, source_text, "
            "updated_by, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,NOW(),NOW())",
            (
                link["airline_id"], fee["fee_id"], fee["label"], fee["basis"], fee["amount"], json.dumps(cond),
                f"{link['name']} (link)"[:500], (user or "link-sync")[:120],
            ),
        )
        conn.commit()
        return cur.lastrowid


# ---------------- Registro y sincronización ----------------

COLS = (
    "id, name, url, origin_code, provider_id, airline_id, commodity_id, valid_days, last_hash, "
    "last_update_date, last_week, last_batch_ids, last_tariffs, last_synced_at, last_status, last_message, created_at, versions"
)


def _stats(batch_ids: list[str]) -> dict:
    """Estado real de las tarifas del link (no lo que dijo el último sync): vigentes, por vencer y vencidas."""
    ids = _tariff_ids(batch_ids)
    out = {"live": 0, "expired": 0, "inactive": 0, "next_expiry": None, "days_left": None}
    if not ids:
        return out
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            cur.execute(
                "SELECT SUM(active = 1 AND f_end >= CURDATE()), SUM(active = 1 AND f_end < CURDATE()), SUM(active = 0), "
                f"MIN(CASE WHEN active = 1 AND f_end >= CURDATE() THEN f_end END) FROM tariffs WHERE id IN ({','.join(['%s'] * len(chunk))})",
                chunk,
            )
            live, expired, inactive, nxt = cur.fetchone()
            out["live"] += int(live or 0)
            out["expired"] += int(expired or 0)
            out["inactive"] += int(inactive or 0)
            nxt = nxt.date() if hasattr(nxt, "date") else nxt
            if nxt and (out["next_expiry"] is None or nxt < out["next_expiry"]):
                out["next_expiry"] = nxt
    if out["next_expiry"]:
        out["days_left"] = (out["next_expiry"] - datetime.now().date()).days
        out["next_expiry"] = out["next_expiry"].isoformat()
    return out


def _tariff_ids(batch_ids: list[str]) -> list[int]:
    if not batch_ids:
        return []
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT tariff_ids FROM airtariff_uploads WHERE status = 'active' AND batch_id IN ({','.join(['%s'] * len(batch_ids))})",
            batch_ids,
        )
        return sorted({int(t) for (raw,) in cur.fetchall() for t in (json.loads(raw) if raw else [])})


def link_tariffs(link_id: int, limit: int = 600) -> dict:
    """Las tarifas que hoy tiene el link (las de sus lotes activos), para seguirlas desde la pestaña Links."""
    ensure_table()
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        batches = json.loads(_get(cur, link_id)[11] or "[]")
    ids = _tariff_ids(batches)
    rows: list[dict] = []
    if ids:
        with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, o.codigo, d.codigo, c.fullname, t.airchaft, t.min, t.forty_five_more, t.hundred_more, "
                "t.three_hundred_more, t.five_hundred_more, t.thousand_more, t.f_end, t.active, t.comments "
                "FROM tariffs t LEFT JOIN transports o ON o.id = t.origin_id LEFT JOIN transports d ON d.id = t.destin_id "
                "LEFT JOIN commodities c ON c.id = t.comodity_id "
                f"WHERE t.id IN ({','.join(['%s'] * len(ids[:limit]))}) ORDER BY d.codigo, c.fullname, t.airchaft",
                ids[:limit],
            )
            for (tid, o, d, com, ac, mn, w45, w100, w300, w500, w1000, f_end, active, comments) in cur.fetchall():
                rows.append({
                    "id": tid, "origin": o, "destination": d, "commodity": com,
                    "aircraft": {1: "CAO", 2: "PAX"}.get(ac, str(ac)),
                    "min": float(mn) if mn is not None else None,
                    "w300": float(w300) if w300 is not None else None, "w500": float(w500) if w500 is not None else None,
                    "w1000": float(w1000) if w1000 is not None else None,
                    "valid_to": f_end.strftime("%Y-%m-%d") if f_end else None, "active": bool(active),
                    "via": (re.search(r"•Product: via (\w{3})", comments or "") or [None, None])[1],
                })
    return {"total": len(ids), "rows": rows}


def link_runs(link_id: int, limit: int = 30) -> list[dict]:
    ensure_table()
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        _get(cur, link_id)  # 404 si no existe
        cur.execute(
            "SELECT status, message, tariffs, week, update_date, run_by, created_at FROM airtariff_link_runs "
            "WHERE link_id = %s ORDER BY id DESC LIMIT %s",
            (link_id, max(1, min(limit, 100))),
        )
        return [
            {
                "status": st, "message": msg, "tariffs": n, "week": wk,
                "update_date": ud.isoformat() if ud else None, "run_by": by,
                "at": at.strftime("%Y-%m-%d %H:%M") if at else None,
            }
            for st, msg, n, wk, ud, by, at in cur.fetchall()
        ]


def _view(r) -> dict:
    (id_, name, url, origin, prov, air, com, days, _hash, upd, wk, batches, n, synced, status, msg, created, versions) = r
    names = tariff_db.company_names([prov, air])
    return {
        "id": id_, "name": name, "url": url, "origin": origin,
        "provider_id": prov, "provider_name": names.get(prov), "airline_id": air, "airline_name": names.get(air),
        "commodity_id": com, "valid_days": days,
        "update_date": upd.isoformat() if upd else None, "week": wk,
        "batch_ids": json.loads(batches) if batches else [], "tariffs": n or 0,
        "last_synced_at": synced.strftime("%Y-%m-%d %H:%M") if synced else None,
        "status": status, "message": msg, "versions": json.loads(versions) if versions else [],
        **_stats(json.loads(batches) if batches else []),
    }


def list_links() -> list[dict]:
    ensure_table()
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {COLS} FROM airtariff_links ORDER BY id DESC")
        return [_view(r) for r in cur.fetchall()]


def _get(cur, link_id: int):
    cur.execute(f"SELECT {COLS} FROM airtariff_links WHERE id = %s", (link_id,))
    r = cur.fetchone()
    if not r:
        raise LinkNotFound(link_id)
    return r


def register(body: dict, created_by: str | None) -> dict:
    """Guarda el link y hace la primera lectura (inserta las tarifas)."""
    ensure_table()
    url = (body.get("url") or "").strip()
    csv_url(url)  # valida el formato antes de guardar
    origin = (body.get("origin") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", origin):
        raise LinkError("The origin must be a 3-letter airport code (e.g. MIA).")
    if origin not in tariff_db.transport_ids([origin]):
        raise LinkError(f"Origin airport {origin} was not found.")
    try:
        provider_id, airline_id, commodity_id = (int(body[k]) for k in ("provider_id", "airline_id", "commodity_id"))
        valid_days = int(body.get("valid_days") or DEFAULT_VALID_DAYS)
    except (KeyError, TypeError, ValueError):
        raise LinkError("Provider, airline and commodity are required.") from None
    if not 1 <= valid_days <= 90:
        raise LinkError("Validity must be between 1 and 90 days.")
    versions = _clean_versions(body.get("versions"))
    name = " ".join(str(body.get("name") or "").split())[:120] or "Spot rates"
    text = fetch_csv(url)
    parse_sheet(text)  # si la hoja no se reconoce no se registra nada
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO airtariff_links (name, url, origin_code, provider_id, airline_id, commodity_id, valid_days, "
            "created_by, versions, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
            (name, url, origin, provider_id, airline_id, commodity_id, valid_days, (created_by or None) and created_by[:120],
             json.dumps(versions) if versions else None),
        )
        link_id = cur.lastrowid
        conn.commit()
    return sync(link_id, force=True, user=created_by, text=text)


def _save_state(link_id: int, _user: str | None = None, **cols) -> None:
    sets = ", ".join(f"{k} = %s" for k in cols)
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE airtariff_links SET {sets}, last_synced_at = NOW() WHERE id = %s", [*cols.values(), link_id])
        if cols.get("last_status"):  # historial: cada revisión queda registrada (qué pasó y cuándo)
            cur.execute(
                "INSERT INTO airtariff_link_runs (link_id, status, message, tariffs, week, update_date, run_by, created_at) "
                "SELECT id, %s, %s, %s, last_week, last_update_date, %s, NOW() FROM airtariff_links WHERE id = %s",
                (cols["last_status"], cols.get("last_message"), cols.get("last_tariffs"), (_user or "")[:120] or None, link_id),
            )
        conn.commit()


def sync(link_id: int, force: bool = False, user: str | None = None, text: str | None = None) -> dict:
    """Revisa la hoja; si cambió (o `force`) reemplaza las tarifas del link. Devuelve el estado del link + `result`."""
    ensure_table()
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        row = _get(cur, link_id)
    link = _view(row)
    old_hash, old_batches = row[8], link["batch_ids"]
    try:
        text = text if text is not None else fetch_csv(link["url"])
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest == old_hash and not force:
            _save_state(link_id, user, last_status="unchanged", last_message="The sheet has not changed.")
            return {**_one(link_id), "result": "unchanged"}
        parsed = parse_sheet(text)
        valid_to = parsed["update_date"] + timedelta(days=link["valid_days"])
        ids = tariff_db.transport_ids([link["origin"], *(r["dest"] for r in parsed["rows"])])
        if link["origin"] not in ids:
            raise LinkError(f"Origin airport {link['origin']} was not found.")
        unknown = sorted({r["dest"] for r in parsed["rows"] if r["dest"] not in ids})
        parsed["rows"] = [r for r in parsed["rows"] if r["dest"] in ids]
        if not parsed["rows"]:
            raise LinkError("None of the destinations exist in transports.")

        # Primero se inserta lo nuevo (un lote por tipo de avión) y después se revierte lo anterior.
        batches: list[str] = []
        total = 0
        comments = _comments(link, parsed)
        for ac, aircraft in AIRCRAFT.items():
            rows = [
                {
                    "origin": link["origin"], "destination": r["dest"], "min": round(r["rate"] * MIN_KG), "n": None,
                    "w45": None, "w100": None, "w300": r["rate"], "w500": r["rate"], "w1000": r["rate"],
                    "note": f"via {r['via']}" if r["via"] else None,
                }
                for r in parsed["rows"] if r["ac"] == ac
            ]
            if not rows:
                continue
            header = {
                "provider_id": link["provider_id"], "airline_id": link["airline_id"], "commodity_id": link["commodity_id"],
                "valid_to": valid_to.isoformat(), "airchaft": aircraft, "comments": comments,
                "source_file": f"{link['name']} (link) WK {parsed['week'] or '?'} {ac}", "uploaded_by": user or "link-sync",
            }
            res = tariff_db.insert_tariffs(header, rows)
            total += res["tariffs"]
            if res.get("batch_id"):
                batches.append(res["batch_id"])

        # Commodities especiales (DG…): mismas tarifas con +pct % (también en el mínimo) y su fee como regla.
        # Si la hoja declara otro porcentaje/monto, manda la hoja.
        sheet_dg = next((x for x in parsed["specials"] if x["kind"] == "dangerous"), None)
        notes_v: list[str] = []
        for v in link["versions"]:
            pct, fee = v["pct"], v.get("fee")
            if sheet_dg and v.get("kind") == "dangerous":
                pct = sheet_dg["pct"] if sheet_dg["pct"] is not None else pct
                if fee and sheet_dg["flat"]:
                    fee = {**fee, "amount": sheet_dg["flat"]}
            factor = 1 + pct / 100
            v_batches: list[str] = []
            for ac, aircraft in AIRCRAFT.items():
                rows = [
                    {
                        "origin": link["origin"], "destination": r["dest"], "min": round(r["rate"] * MIN_KG * factor),
                        "n": None, "w45": None, "w100": None, "w300": round(r["rate"] * factor, 2),
                        "w500": round(r["rate"] * factor, 2), "w1000": round(r["rate"] * factor, 2),
                        "note": f"via {r['via']}" if r["via"] else None,
                    }
                    for r in parsed["rows"] if r["ac"] == ac
                ]
                if not rows:
                    continue
                header = {
                    "provider_id": link["provider_id"], "airline_id": link["airline_id"],
                    "commodity_id": v["commodity_id"], "valid_to": valid_to.isoformat(), "airchaft": aircraft,
                    "comments": comments, "uploaded_by": user or "link-sync",
                    "source_file": f"{link['name']} (link) WK {parsed['week'] or '?'} {ac} +{pct:g}%",
                }
                res = tariff_db.insert_tariffs(header, rows)
                total += res["tariffs"]
                if res.get("batch_id"):
                    v_batches.append(res["batch_id"])
            batches.extend(v_batches)
            if fee and v_batches:
                rule_id = _create_fee_rule(link, v, fee, user)
                upload_history.attach_rules(v_batches[0], [rule_id])
            notes_v.append(f"+{pct:g}%" + (f" and fee {fee['amount']:g}" if fee else ""))

        for b in old_batches:
            try:
                upload_history.revert_batch(b, user or "link-sync")
            except (upload_history.BatchNotFound, upload_history.AlreadyReverted):
                pass  # ya no está o alguien lo revirtió a mano

        msg = f"{total} tariffs loaded (valid to {valid_to})."
        if notes_v:
            msg += f" Special commodities: {', '.join(notes_v)}."
        if unknown:
            msg += f" Skipped (not in transports): {', '.join(unknown)}."
        if parsed["closed"]:
            msg += f" Closed: {', '.join(parsed['closed'])}."
        if valid_to < datetime.now().date():
            msg += " The sheet's update date is old, so these tariffs are already expired."
        _save_state(
            link_id, user, last_hash=digest, last_update_date=parsed["update_date"], last_week=parsed["week"],
            last_batch_ids=json.dumps(batches), last_tariffs=total, last_status="updated", last_message=msg[:500],
        )
        return {**_one(link_id), "result": "updated"}
    except (LinkError, ValueError, mysql.connector.errors.Error) as e:
        _save_state(link_id, user, last_status="error", last_message=str(e)[:500])
        raise


def _one(link_id: int) -> dict:
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        return _view(_get(cur, link_id))


def sync_all() -> list[dict]:
    """Revisa todos los links (solo actualiza los que cambiaron). Un link con error no frena a los demás."""
    out = []
    for link in list_links():
        try:
            out.append(sync(link["id"]))
        except (LinkError, ValueError, mysql.connector.errors.Error):
            out.append(_one(link["id"]))
    return out
