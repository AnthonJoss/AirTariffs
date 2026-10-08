"""Acceso a MySQL para tarifas aéreas (usa db_conn.py, perfil local por defecto)."""
import html
import json
import os

import mysql.connector

import tariff_archive
import upload_history
from db_conn import db_conn

# En Cloud Run MYSQL_PROFILE=remote (Cloud SQL); en local queda "local".
PROFILE = os.getenv("MYSQL_PROFILE", "local")

TYPE_TARIFF_AIR = 3
DEFAULT_USER_ID = 1
DEFAULT_COMMODITY_ID = 4  # Freight of All Kind


def search_companies(q: str, limit: int = 15, type_id: int | None = None):
    """Empresas por nombre. type_id filtra por tipo (company_types.type_id): 6 = Air Carrier."""
    sql = "SELECT c.id, c.fullname FROM companies c WHERE c.fullname LIKE %s"
    params: list = [f"%{q}%"]
    if type_id is not None:
        sql += " AND EXISTS (SELECT 1 FROM company_types ct WHERE ct.company_id = c.id AND ct.type_id = %s)"
        params.append(type_id)
    sql += " ORDER BY c.fullname LIMIT %s"
    params.append(limit)
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [{"id": i, "name": n} for i, n in cur.fetchall()]


def search_airports(q: str, limit: int = 15):
    """Aeropuertos (transports.type_id = 1) por código o nombre; los que empiezan por el texto van primero."""
    q = (q or "").strip()
    if not q:
        return []
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, codigo, fullname FROM transports WHERE type_id = 1 AND (codigo LIKE %s OR fullname LIKE %s) "
            "ORDER BY (codigo = %s) DESC, (codigo LIKE %s) DESC, fullname LIMIT %s",
            (f"{q}%", f"%{q}%", q.upper(), f"{q}%", limit),
        )
        return [{"id": i, "code": (c or "").upper(), "name": n} for i, c, n in cur.fetchall()]


def company_names(ids) -> dict[int, str]:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id, fullname FROM companies WHERE id IN ({','.join(['%s'] * len(ids))})", ids)
        return dict(cur.fetchall())


def get_client_terms(company_ids) -> list[dict]:
    """Términos guardados (airtariff_client_terms) de esas empresas, con el texto de sus archivos."""
    ids = sorted({int(i) for i in company_ids if i})
    if not ids:
        return []
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT company_id, company_name, instructions, terms_files, updated_by, updated_at "
                f"FROM airtariff_client_terms WHERE company_id IN ({','.join(['%s'] * len(ids))})",
                ids,
            )
        except mysql.connector.errors.ProgrammingError as e:
            # 1146 = la tabla aun no existe en esa BD: se analiza sin terminos en vez de fallar.
            if e.errno == 1146:
                return []
            raise
        return [
            {
                "company_id": cid,
                "company_name": name,
                "instructions": instr or "",
                "files": json.loads(files) if files else [],
                "updated_by": by,
                "updated_at": at.strftime("%Y-%m-%d %H:%M") if at else None,
            }
            for cid, name, instr, files, by, at in cur.fetchall()
        ]


def save_client_terms(company_id: int, company_name: str, instructions: str, files: list[dict], updated_by: str):
    """Crea o reemplaza los términos de una empresa (una fila por company_id)."""
    # Sin VALUES() en el UPDATE: MySQL 8 lo marca deprecado y raise_on_warnings lo vuelve error.
    vals = (company_name, instructions, json.dumps(files, ensure_ascii=False), updated_by)
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO airtariff_client_terms
                 (company_id, company_name, instructions, terms_files, updated_by, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
               ON DUPLICATE KEY UPDATE company_name=%s, instructions=%s, terms_files=%s,
                 updated_by=%s, updated_at=NOW()""",
            (company_id, *vals, *vals),
        )
        conn.commit()


def fees_catalog():
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, code, fullname FROM fees ORDER BY (type_tariff=3) DESC, fullname")
        return [{"id": i, "code": c, "name": n} for i, c, n in cur.fetchall()]


def air_fee_ids() -> set[int]:
    """Fees que de verdad se usan en tarifas aéreas (tariff_feeds de tariffs type 3)."""
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT tf.fee_id FROM tariff_feeds tf JOIN tariffs t ON t.id = tf.tariff_id "
            "WHERE t.type_tariff = %s AND tf.fee_id IS NOT NULL",
            (TYPE_TARIFF_AIR,),
        )
        return {i for (i,) in cur.fetchall()}


def commodities():
    """Commodities de carga aérea: `commodities.mode = 'air'` (GEN, DG, PER, AVI, PIL).

    La tabla es compartida con marítimo; esos no se ofrecen acá. Si la columna `mode`
    aún no existe en la BD (SQL de Cloud SQL sin aplicar) se devuelven todos, como antes.
    """
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        try:
            cur.execute("SELECT id, fullname FROM commodities WHERE mode = 'air' ORDER BY fullname")
        except mysql.connector.errors.ProgrammingError as e:
            if e.errno != 1054:  # 1054 = Unknown column
                raise
            cur.execute("SELECT id, fullname FROM commodities ORDER BY fullname")
        return [{"id": i, "name": n} for i, n in cur.fetchall()]


def create_commodity(name: str) -> dict:
    """Crea un commodity de carga aérea (`commodities.mode = 'air'`) o devuelve el que ya existe con ese nombre.

    Sirve para libros con una hoja por producto (Emirates: AOG, VAL, MUW…) cuyos productos no están en el
    catálogo. Solo se compara contra los commodities aéreos: uno marítimo con el mismo nombre no se toca.
    """
    name = " ".join((name or "").split())
    if not 2 <= len(name) <= 60:
        raise ValueError("The commodity name must have between 2 and 60 characters.")
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        try:
            cur.execute("SELECT id, fullname FROM commodities WHERE mode = 'air' AND LOWER(fullname) = LOWER(%s) LIMIT 1", (name,))
        except mysql.connector.errors.ProgrammingError as e:
            if e.errno == 1054:  # la columna `mode` aún no existe
                raise RuntimeError("commodities.mode does not exist yet: apply the SQL of the air commodities first.") from e
            raise
        row = cur.fetchone()
        if row:
            return {"id": row[0], "name": row[1], "created": False}
        cur.execute("INSERT INTO commodities (fullname, valid, mode) VALUES (%s, 1, 'air')", (name,))
        conn.commit()
        return {"id": cur.lastrowid, "name": name, "created": True}


def transport_ids(codes) -> dict[str, int]:
    codes = sorted({c for c in codes if c})
    if not codes:
        return {}
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        # Solo aeropuertos (type_id = 1): ciudades/zips (3) y puertos (2) también usan `codigo`
        # y podían ganarle al aeropuerto con la misma sigla. ORDER BY id: con varias filas del
        # mismo aeropuerto (ABE = Allentown/Bethlehem/Easton) gana siempre la misma (la última).
        cur.execute(
            f"SELECT id, codigo FROM transports WHERE type_id = 1 AND codigo IN ({','.join(['%s'] * len(codes))}) ORDER BY id",
            codes,
        )
        return {str(c).upper(): i for i, c in cur.fetchall()}


def existing_count(provider_id, airline_id, commodity_id, pairs) -> int:
    """Cuántas de las rutas ya tienen una tarifa aérea activa y vigente (aviso de duplicado)."""
    if not pairs:
        return 0
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT origin_id, destin_id FROM tariffs
               WHERE type_tariff=%s AND active=1 AND f_end >= NOW()
                 AND provider_id=%s AND airline_id=%s AND comodity_id=%s""",
            (TYPE_TARIFF_AIR, provider_id, airline_id, commodity_id),
        )
        have = set(cur.fetchall())
    return sum(1 for p in pairs if p in have)


INSERT_SQL = """
INSERT INTO tariffs
  (origin_id, destin_id, provider_id, airline_id, comodity_id,
   min, n, comments, active, user_id, type_tariff,
   forty_five_more, hundred_more, three_hundred_more, five_hundred_more, thousand_more,
   f_end, created_at, updated_at, airchaft, cost, offer)
VALUES (%s,%s,%s,%s,%s, %s,%s,%s,1,%s,%s, %s,%s,%s,%s,%s, %s,NOW(),NOW(), %s,%s,%s)
"""


FEE_SQL = """
INSERT INTO tariff_feeds (fee_id, fee_comment, units, cost_unit, cost, tariff_id)
VALUES (%s, %s, 1, %s, %s, %s)
"""


def row_comments(header_comments, note) -> str | None:
    """Comentarios de una tarifa: el nombre de su producto en el archivo ("Product: AC DGR - Standard") y,
    después, los comentarios generales del archivo. Así el nombre queda en CADA tarifa aunque varios
    productos compartan commodity, y el buscador de tarifas lo muestra (AirTariffSearch::productNote)."""
    note = " ".join(str(note or "").split())[:120]
    if not note:
        return header_comments
    return f"<p>•Product: {html.escape(note)}</p>" + (header_comments or "")


def insert_tariffs(header: dict, rows: list[dict]) -> dict:
    """Inserta todas las filas en una sola transacción (todo o nada)."""
    ids = transport_ids([r["origin"] for r in rows] + [r["destination"] for r in rows])
    missing = sorted({c for r in rows for c in (r["origin"], r["destination"]) if c not in ids})
    if missing:
        raise ValueError(f"Aeropuertos sin ID en transports: {', '.join(missing)}")

    adj = float(header.get("min_adjust") or 0)
    data = []
    for r in rows:
        if r.get("min") is None:
            raise ValueError(f"{r['origin']}-{r['destination']}: falta el MIN")
        data.append((
            ids[r["origin"]], ids[r["destination"]], header["provider_id"], header["airline_id"],
            header["commodity_id"], round(float(r["min"]) + adj), r["n"], row_comments(header.get("comments"), r.get("note")),
            header.get("user_id", DEFAULT_USER_ID), TYPE_TARIFF_AIR,
            r["w45"], r["w100"], r["w300"], r["w500"], r["w1000"],
            header["valid_to"],
            header.get("airchaft", 2), header.get("cost", 0), header.get("offer", 1),
        ))

    fees = header.get("fees") or []
    fuel_fee_id = header.get("fuel_fee_id")
    upload_history.ensure_table()  # antes de la transaccion: un CREATE TABLE haria commit implicito
    replace = tariff_archive.clean_spec(header.get("replace"))  # valida antes de insertar nada
    if replace:
        tariff_archive.ensure_table()
    skip_rule_ids = upload_history.clean_skip_rule_ids(header.get("skip_rule_ids"))
    fee_count = 0
    archived = 0
    tariff_ids: list[int] = []
    with db_conn(PROFILE, pooled=False) as conn:
        cur = conn.cursor()
        try:
            for row, vals in zip(rows, data):
                cur.execute(INSERT_SQL, vals)
                tariff_id = cur.lastrowid
                tariff_ids.append(tariff_id)
                # Fees generales (header, todas las filas) + individuales de esta fila.
                row_fees = [
                    (f["fee_id"], f.get("fee_comment"), f["cost_unit"])
                    for f in fees + (row.get("fees") or [])
                ]
                if fuel_fee_id and row.get("fuel"):
                    row_fees.append((fuel_fee_id, None, row["fuel"]))
                for fee_id, comment, cost in row_fees:
                    cur.execute(FEE_SQL, (fee_id, comment, cost, cost, tariff_id))
                    fee_count += 1
            # Historial del upload (para poder revertirlo) en la MISMA transaccion: o entra todo o nada.
            batch_id = upload_history.record_upload(cur, header, tariff_ids, fee_count)
            # Reglas de fees de la aerolínea que esta subida NO debe recibir (misma transacción).
            upload_history.record_rule_skips(cur, header.get("airline_id"), tariff_ids, skip_rule_ids)
            # "Reemplazar lo anterior": lo que sustituye esta subida pasa al histórico en la MISMA transacción.
            if replace:
                pairs = [(ids[r["origin"]], ids[r["destination"]]) for r in rows]
                archived = tariff_archive.archive_for_replace(cur, header, replace, tariff_ids, pairs, batch_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    return {
        "tariffs": len(data), "fees": fee_count, "batch_id": batch_id, "tracked": batch_id is not None,
        "archived": archived,
    }
