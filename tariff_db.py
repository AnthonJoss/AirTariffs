"""Acceso a MySQL para tarifas aéreas (usa db_conn.py, perfil local por defecto)."""
from db_conn import db_conn

PROFILE = "local"

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
                row_fees = [(f["fee_id"], f.get("fee_comment"), f["cost_unit"]) for f in fees]
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
