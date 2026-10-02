"""Conexión a MySQL por perfil, configurable por variables de entorno.

Perfiles:
- local : localhost:3307 (root)
- remote: Cloud SQL next-logistics-instance (nextlog_admin)

Mismas variables que next-notice-carrier en Cloud Run, todas opcionales:
MYSQL_HOST_OVERRIDE, MYSQL_PORT_OVERRIDE, MYSQL_USER_OVERRIDE,
MYSQL_PASSWORD_OVERRIDE, MYSQL_DB_OVERRIDE y MYSQL_SOCKET_OVERRIDE
(en Cloud Run: /cloudsql/<instancia>, tiene prioridad sobre host/port).
Sin secretos en el código: la contraseña va en `.env` (local) o Secret Manager.
"""
import os
from contextlib import contextmanager
from pathlib import Path

import mysql.connector

PROFILES = {
    "local": dict(host="localhost", port=3307, user="root"),
    "remote": dict(host="34.138.138.93", port=3306, user="nextlog_admin"),
}


def _load_env():
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def _config(profile: str) -> dict:
    if profile not in PROFILES:
        raise ValueError(f"Perfil desconocido: {profile!r}. Usa 'remote' o 'local'.")
    _load_env()
    cfg = dict(
        PROFILES[profile],
        password=os.getenv("MYSQL_PASSWORD_OVERRIDE", ""),
        database=os.getenv("MYSQL_DB_OVERRIDE", "nextlog_logistic"),
        connection_timeout=15,
        use_unicode=True,
        charset="utf8mb4",
        autocommit=False,
        raise_on_warnings=True,
    )
    if os.getenv("MYSQL_HOST_OVERRIDE"):
        cfg["host"] = os.environ["MYSQL_HOST_OVERRIDE"]
    if os.getenv("MYSQL_PORT_OVERRIDE"):
        cfg["port"] = int(os.environ["MYSQL_PORT_OVERRIDE"])
    if os.getenv("MYSQL_USER_OVERRIDE"):
        cfg["user"] = os.environ["MYSQL_USER_OVERRIDE"]
    if os.getenv("MYSQL_SOCKET_OVERRIDE"):
        cfg["unix_socket"] = os.environ["MYSQL_SOCKET_OVERRIDE"]
        cfg.pop("host")
        cfg.pop("port")
    return cfg


@contextmanager
def db_conn(profile: str = "local", pooled: bool = False):
    """`with db_conn("local") as conn:` — `pooled` se acepta por compatibilidad con MailReader."""
    conn = mysql.connector.connect(**_config(profile))
    try:
        yield conn
    finally:
        conn.close()
