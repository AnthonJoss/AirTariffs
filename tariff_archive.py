"""Histórico de tarifas: mover tarifas vigentes a un archivo ("history") sin borrarlas, y devolverlas.

Archivar = `tariffs.active = 0` + un renglón en `airtariff_archive` (qué tarifa, por qué, quién, cuándo y qué
lote la reemplazó). Las búsquedas solo muestran `active = 1`, así que lo archivado sale de Tariffs/Quote pero las
cotizaciones viejas siguen apuntando a su tarifa. Restaurar la vuelve a `active = 1`.

Tres formas de llegar acá:
- Al insertar (`header.replace`, dentro de la MISMA transacción del insert: todo o nada) con un modo:
  `same_routes` (misma aerolínea+proveedor+commodity+avión y los pares origen→destino del archivo nuevo),
  `whole_airline` (todo lo vigente de esa aerolínea+proveedor+commodity), `airline_all` (todos los commodities de la
  aerolínea+proveedor) o `batches` (los lotes/uploads que el usuario elija). `any_provider` ignora el proveedor.
  Nunca toca lo que insertó la misma tanda (`run_id`/`batch_id`), así que subir varios PDFs o versiones juntos no
  se archiva entre sí.
- A mano (`archive_manual`): una tanda (lotes) o tarifas sueltas pasan al histórico sin subir nada nuevo.
- Al revertir el lote que las reemplazó (`restore_by_replacer`, lo llama `upload_history.revert_batch`).

La tabla (`airtariff_archive`) la crea este servicio la primera vez (`ensure_table`).
"""
import json
import os
from datetime import datetime

import mysql.connector

from db_conn import db_conn

PROFILE = os.getenv("MYSQL_PROFILE", "local")
TYPE_TARIFF_AIR = 3
CHUNK = 400
MODES = ("same_routes", "whole_airline", "airline_all", "batches")
HISTORY_LIMIT = 2000

SCHEMA = """
CREATE TABLE airtariff_archive (
    id                INT UNSIGNED NOT NULL AUTO_INCREMENT,
    tariff_id         INT NOT NULL,
    airline_id        INT NULL,
    reason            VARCHAR(24)  NOT NULL,
    replaced_by_batch VARCHAR(36)  NULL,
    archived_by       VARCHAR(120) NULL,
    archived_at       TIMESTAMP NULL,
    restored_at       TIMESTAMP NULL,
    restored_by       VARCHAR(120) NULL,
    PRIMARY KEY (id),
    KEY airtariff_archive_tariff_index (tariff_id),
    KEY airtariff_archive_airline_index (airline_id, archived_at),
    KEY airtariff_archive_batch_index (replaced_by_batch)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_table_ready = False


class ArchiveError(ValueError):
    pass


def ensure_table() -> None:
    """Crea la tabla si falta (una vez por proceso). El CREATE hace commit implícito: va FUERA de transacciones."""
    global _table_ready
    if _table_ready:
        return
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'airtariff_archive'"
        )
        if not cur.fetchone():
            cur.execute(SCHEMA)
        conn.commit()
    _table_ready = True


def _marks(n: int) -> str:
    return ",".join(["%s"] * n)


def _chunks(items: list, size: int = CHUNK):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def clean_spec(raw) -> dict | None:
    """Valida `replace`: {mode, batch_ids?, any_provider?}. None = no reemplaza nada."""
    if not raw or raw.get("mode") in (None, "", "none"):
        return None
    mode = raw["mode"]
    if mode not in MODES:
        raise ArchiveError(f"Unknown replace mode: {mode}")
    batches = [str(b)[:36] for b in (raw.get("batch_ids") or []) if b]
    if mode == "batches" and not batches:
        raise ArchiveError("Pick at least one previous upload to replace.")
    return {"mode": mode, "batch_ids": batches, "any_provider": bool(raw.get("any_provider"))}


def _tariff_ids_of_batches(cur, batch_ids: list[str]) -> list[int]:
    if not batch_ids:
        return []
    cur.execute(
        f"SELECT tariff_ids FROM airtariff_uploads WHERE status = 'active' AND batch_id IN ({_marks(len(batch_ids))})",
        batch_ids,
    )
    return sorted({int(t) for (raw,) in cur.fetchall() for t in (json.loads(raw) if raw else [])})


def _run_ids(cur, run_id: str | None, batch_id: str | None, new_ids: list[int]) -> set[int]:
    """Lo que NO se archiva: lo recién insertado y todo lo de la misma tanda (mismo run_id o batch_id)."""
    keep = set(new_ids)
    for col, val in (("run_id", run_id), ("batch_id", batch_id)):
        if val:
            cur.execute(f"SELECT tariff_ids FROM airtariff_uploads WHERE {col} = %s", (val,))
            keep.update(int(t) for (raw,) in cur.fetchall() for t in (json.loads(raw) if raw else []))
    return keep


def find_targets(cur, ctx: dict, spec: dict, pairs: list[tuple[int, int]], keep: set[int]) -> list[int]:
    """Ids de las tarifas vigentes (active = 1) que `spec` mandaría al histórico."""
    mode = spec["mode"]
    if mode == "batches":
        cand = _tariff_ids_of_batches(cur, spec["batch_ids"])
        out: list[int] = []
        for chunk in _chunks(cand):
            cur.execute(
                f"SELECT id FROM tariffs WHERE type_tariff = %s AND active = 1 AND id IN ({_marks(len(chunk))})",
                [TYPE_TARIFF_AIR, *chunk],
            )
            out += [r[0] for r in cur.fetchall()]
        return sorted(set(out) - keep)

    where = ["t.type_tariff = %s", "t.active = 1", "t.airline_id = %s"]
    params: list = [TYPE_TARIFF_AIR, ctx["airline_id"]]
    if not spec["any_provider"]:
        where.append("t.provider_id = %s")
        params.append(ctx["provider_id"])
    if mode in ("same_routes", "whole_airline"):
        where.append("t.comodity_id = %s")
        params.append(ctx["commodity_id"])
    if mode == "same_routes":
        where.append("t.airchaft = %s")
        params.append(ctx["airchaft"])
    if mode != "same_routes":
        cur.execute(f"SELECT t.id FROM tariffs t WHERE {' AND '.join(where)}", params)
        return sorted({r[0] for r in cur.fetchall()} - keep)
    out = set()
    for chunk in _chunks(sorted(set(pairs))):
        cond = " OR ".join(["(t.origin_id = %s AND t.destin_id = %s)"] * len(chunk))
        cur.execute(
            f"SELECT t.id FROM tariffs t WHERE {' AND '.join(where)} AND ({cond})",
            [*params, *(v for p in chunk for v in p)],
        )
        out.update(r[0] for r in cur.fetchall())
    return sorted(out - keep)


def _archive(cur, ids: list[int], airline_id, reason: str, by: str | None, replaced_by: str | None) -> None:
    for chunk in _chunks(ids):
        cur.execute(f"UPDATE tariffs SET active = 0, updated_at = NOW() WHERE id IN ({_marks(len(chunk))})", chunk)
        cur.executemany(
            "INSERT INTO airtariff_archive (tariff_id, airline_id, reason, replaced_by_batch, archived_by, archived_at) "
            "VALUES (%s,%s,%s,%s,%s,NOW())",
            [(i, airline_id, reason, replaced_by, (by or None) and by[:120]) for i in chunk],
        )


def archive_for_replace(cur, header: dict, spec: dict, new_ids: list[int], pairs: list[tuple[int, int]], batch_id: str | None) -> int:
    """Dentro de la transacción del insert: manda al histórico lo que reemplaza la subida nueva. Devuelve cuántas."""
    keep = _run_ids(cur, header.get("run_id"), batch_id, new_ids)
    ctx = {
        "airline_id": header["airline_id"], "provider_id": header["provider_id"],
        "commodity_id": header["commodity_id"], "airchaft": header.get("airchaft", 2),
    }
    ids = find_targets(cur, ctx, spec, pairs, keep)
    _archive(cur, ids, header["airline_id"], f"replace_{spec['mode']}"[:24], header.get("uploaded_by"), batch_id)
    return len(ids)


def preview_replace(body: dict) -> dict:
    """Cuántas tarifas pasarían al histórico (sin tocar nada) y de qué subidas vienen."""
    from tariff_db import transport_ids  # import local: tariff_db importa este módulo

    spec = clean_spec(body.get("replace"))
    if not spec:
        return {"total": 0, "uploads": [], "routes": []}
    try:
        ctx = {
            "airline_id": int(body["airline_id"]), "provider_id": int(body.get("provider_id") or 0),
            "commodity_id": int(body.get("commodity_id") or 0), "airchaft": int(body.get("airchaft") or 2),
        }
    except (KeyError, TypeError, ValueError):
        raise ArchiveError("airline_id is required.") from None
    codes = sorted({c for p in body.get("pairs") or [] for c in p if c})
    ids = transport_ids(codes)
    pairs = [(ids[o], ids[d]) for o, d in (body.get("pairs") or []) if o in ids and d in ids]
    # El archivo se sube en su commodity base y, si tiene "también como otros commodities", en cada versión
    # (cada una reemplaza lo suyo): se suman todos. airline_all y batches no dependen del commodity.
    commodity_ids = [int(c) for c in (body.get("commodity_ids") or []) if c] or [ctx["commodity_id"]]
    if spec["mode"] in ("airline_all", "batches"):
        commodity_ids = commodity_ids[:1]
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        keep = _run_ids(cur, body.get("run_id"), body.get("batch_id"), [])
        targets: set[int] = set()
        by_commodity: dict[int, int] = {}
        for cid in dict.fromkeys(commodity_ids):
            found = set(find_targets(cur, {**ctx, "commodity_id": cid}, spec, pairs, keep)) - targets
            by_commodity[cid] = len(found)
            targets |= found
        out = _summary(cur, sorted(targets))
        if len(by_commodity) > 1 or spec["mode"] in ("same_routes", "whole_airline"):
            names = {}
            if by_commodity:
                cur.execute(
                    f"SELECT id, fullname FROM commodities WHERE id IN ({_marks(len(by_commodity))})", list(by_commodity)
                )
                names = dict(cur.fetchall())
            out["by_commodity"] = [
                {"commodity_id": c, "commodity": names.get(c, f"#{c}"), "tariffs": n} for c, n in by_commodity.items()
            ]
        return out


def _summary(cur, ids: list[int]) -> dict:
    """Resumen de un conjunto de tarifas: por subida de origen y por ruta (ejemplos)."""
    uploads: dict[str, dict] = {}
    routes: list[str] = []
    for chunk in _chunks(ids):
        cur.execute(
            "SELECT t.id, o.codigo, d.codigo FROM tariffs t LEFT JOIN transports o ON o.id = t.origin_id "
            f"LEFT JOIN transports d ON d.id = t.destin_id WHERE t.id IN ({_marks(len(chunk))})",
            chunk,
        )
        routes += [f"{o}→{d}" for _, o, d in cur.fetchall()]
    if ids:
        cur.execute("SELECT batch_id, file_name, created_at, tariff_ids FROM airtariff_uploads WHERE status = 'active'")
        idset = set(ids)
        for batch, fname, created, raw in cur.fetchall():
            n = len(idset & {int(t) for t in (json.loads(raw) if raw else [])})
            if n:
                u = uploads.setdefault(batch, {"batch_id": batch, "file_name": fname, "created_at": None, "tariffs": 0})
                u["tariffs"] += n
                u["created_at"] = created.strftime("%Y-%m-%d") if created else None
    return {
        "total": len(ids),
        "uploads": sorted(uploads.values(), key=lambda u: u["created_at"] or "", reverse=True),
        "routes": sorted(set(routes))[:40],
    }


def archive_manual(batch_ids: list[str], tariff_ids: list[int], by: str | None, dry_run: bool = False) -> dict:
    """Pasa al histórico una tanda (lotes) o tarifas sueltas, sin subir nada nuevo."""
    if not batch_ids and not tariff_ids:
        raise ArchiveError("Pick uploads or tariffs to move to history.")
    ensure_table()
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cand = sorted({*(int(t) for t in tariff_ids), *_tariff_ids_of_batches(cur, [str(b) for b in batch_ids])})
        ids: list[int] = []
        airlines: dict[int, int | None] = {}
        for chunk in _chunks(cand):
            cur.execute(
                f"SELECT id, airline_id FROM tariffs WHERE type_tariff = %s AND active = 1 AND id IN ({_marks(len(chunk))})",
                [TYPE_TARIFF_AIR, *chunk],
            )
            for i, a in cur.fetchall():
                ids.append(i)
                airlines[i] = a
        result = {"dry_run": dry_run, **_summary(cur, ids)}
        if dry_run or not ids:
            return result
        try:
            for a in {airlines[i] for i in ids}:
                _archive(cur, [i for i in ids if airlines[i] == a], a, "manual", by, None)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result


# ---------------- Consulta y restauración ----------------

def history(airline_id: int, limit: int = HISTORY_LIMIT) -> dict:
    """Tarifas de una aerolínea que están en el histórico, con por qué y qué subida las reemplazó, y los grupos."""
    ensure_table()
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT a.id, a.tariff_id, a.reason, a.archived_by, a.archived_at, a.replaced_by_batch, "
            "(SELECT u.file_name FROM airtariff_uploads u WHERE u.batch_id = a.replaced_by_batch ORDER BY u.id LIMIT 1), "
            "o.codigo, d.codigo, c.fullname, t.airchaft, t.min, t.three_hundred_more, t.hundred_more, t.n, t.f_end, "
            "p.fullname FROM airtariff_archive a JOIN tariffs t ON t.id = a.tariff_id "
            "LEFT JOIN transports o ON o.id = t.origin_id LEFT JOIN transports d ON d.id = t.destin_id "
            "LEFT JOIN commodities c ON c.id = t.comodity_id LEFT JOIN companies p ON p.id = t.provider_id "
            "WHERE a.airline_id = %s AND a.restored_at IS NULL AND t.active = 0 "
            "ORDER BY a.archived_at DESC, d.codigo LIMIT %s",
            (airline_id, max(1, min(limit, 5000))),
        )
        raw = cur.fetchall()
    groups: dict[str, dict] = {}
    rows = []
    num = lambda v: float(v) if v is not None else None  # noqa: E731
    for (aid, tid, reason, by, at, batch, fname, o, d, com, ac, mn, w300, w100, n, f_end, prov) in raw:
        key = f"{batch or 'manual'}|{at.strftime('%Y%m%d%H%M') if at else ''}|{reason}"
        g = groups.setdefault(key, {
            "key": key, "reason": reason, "archived_by": by, "archived_at": at.strftime("%Y-%m-%d %H:%M") if at else None,
            "replaced_by": fname, "replaced_by_batch": batch, "tariffs": 0,
        })
        g["tariffs"] += 1
        rows.append({
            "archive_id": aid, "tariff_id": tid, "group": key, "origin": o, "destination": d, "commodity": com,
            "provider": prov, "aircraft": {1: "CAO", 2: "PAX"}.get(ac, str(ac)), "min": num(mn), "n": num(n),
            "w100": num(w100), "w300": num(w300), "valid_to": f_end.strftime("%Y-%m-%d") if f_end else None,
        })
    return {"groups": list(groups.values()), "rows": rows}


def restore(archive_ids: list[int], by: str | None) -> dict:
    """Devuelve tarifas del histórico a vigentes (active = 1). Si la ruta ya tiene otra vigente, quedan las dos."""
    ids = sorted({int(i) for i in archive_ids})
    if not ids:
        raise ArchiveError("Pick tariffs to restore.")
    ensure_table()
    with db_conn(PROFILE, pooled=False) as conn, conn.cursor() as cur:
        try:
            n = _restore(cur, f"a.id IN ({_marks(len(ids))})", ids, by)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"restored": n}


def _restore(cur, where: str, params: list, by: str | None) -> int:
    cur.execute(f"SELECT a.id, a.tariff_id FROM airtariff_archive a WHERE a.restored_at IS NULL AND {where}", params)
    found = cur.fetchall()
    if not found:
        return 0
    for chunk in _chunks(found):
        cur.execute(
            f"UPDATE tariffs SET active = 1, updated_at = NOW() WHERE id IN ({_marks(len(chunk))})",
            [t for _, t in chunk],
        )
        cur.execute(
            f"UPDATE airtariff_archive SET restored_at = NOW(), restored_by = %s WHERE id IN ({_marks(len(chunk))})",
            [(by or None) and by[:120], *(a for a, _ in chunk)],
        )
    return len(found)


def restore_by_replacer(cur, batch_id: str, by: str | None) -> int:
    """Al revertir el lote que reemplazó tarifas, esas tarifas vuelven a estar vigentes (misma transacción)."""
    try:
        return _restore(cur, "a.replaced_by_batch = %s", [batch_id], by)
    except mysql.connector.errors.ProgrammingError as e:
        if e.errno == 1146:  # la tabla aún no existe: nada se archivó
            return 0
        raise
