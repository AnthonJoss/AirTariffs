"""Acceso a MySQL para tarifas aéreas (usa db_conn.py, perfil local por defecto)."""
import json
import os

import mysql.connector

from db_conn import db_conn

# En Cloud Run MYSQL_PROFILE=remote (Cloud SQL); en local queda "local".
PROFILE = os.getenv("MYSQL_PROFILE", "local")

TYPE_TARIFF_AIR = 3
DEFAULT_USER_ID = 1
DEFAULT_COMMODITY_ID = 4  # Freight of All Kind


def search_companies(q: str, limit: int = 15):
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, fullname FROM companies WHERE fullname LIKE %s ORDER BY fullname LIMIT %s",
            (f"%{q}%", limit),
        )
        return [{"id": i, "name": n} for i, n in cur.fetchall()]


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


def commodities():
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, fullname FROM commodities ORDER BY fullname")
        return [{"id": i, "name": n} for i, n in cur.fetchall()]


def transport_ids(codes) -> dict[str, int]:
    codes = sorted({c for c in codes if c})
    if not codes:
        return {}
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT id, codigo FROM transports WHERE codigo IN ({','.join(['%s'] * len(codes))})", codes
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
            header["commodity_id"], round(float(r["min"]) + adj), r["n"], header.get("comments"),
            header.get("user_id", DEFAULT_USER_ID), TYPE_TARIFF_AIR,
            r["w45"], r["w100"], r["w300"], r["w500"], r["w1000"],
            header["valid_to"],
            header.get("airchaft", 2), header.get("cost", 0), header.get("offer", 1),
        ))

    fees = header.get("fees") or []
    fuel_fee_id = header.get("fuel_fee_id")
    fee_count = 0
    with db_conn(PROFILE, pooled=False) as conn:
        cur = conn.cursor()
        try:
            for row, vals in zip(rows, data):
                cur.execute(INSERT_SQL, vals)
                tariff_id = cur.lastrowid
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
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    return {"tariffs": len(data), "fees": fee_count}
