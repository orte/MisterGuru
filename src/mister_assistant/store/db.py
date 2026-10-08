"""Conexión a Postgres y migraciones SQL versionadas (supabase/migrations)."""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "supabase" / "migrations"


def connect(dsn: str) -> psycopg.Connection[tuple[object, ...]]:
    # autocommit + bloques `conn.transaction()` explícitos en cada paso de los jobs.
    # prepare_threshold=None: el pooler de Supabase (modo transacción) no admite
    # sentencias preparadas del lado servidor.
    return psycopg.connect(dsn, autocommit=True, prepare_threshold=None, connect_timeout=15)


def pending_migrations(
    conn: psycopg.Connection[tuple[object, ...]], directory: Path = MIGRATIONS_DIR
) -> list[Path]:
    _ensure_table(conn)
    applied = {row[0] for row in conn.execute("select version from schema_migrations")}
    return [p for p in sorted(directory.glob("*.sql")) if p.stem not in applied]


def migrate(
    conn: psycopg.Connection[tuple[object, ...]], directory: Path = MIGRATIONS_DIR
) -> list[str]:
    """Aplica en orden las migraciones pendientes, cada una en su transacción."""
    done: list[str] = []
    for path in pending_migrations(conn, directory):
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("insert into schema_migrations (version) values (%s)", (path.stem,))
        log.info("migración aplicada: %s", path.name)
        done.append(path.stem)
    return done


def _ensure_table(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    with conn.transaction():
        conn.execute(
            "create table if not exists schema_migrations ("
            " version text primary key, applied_at timestamptz not null default now())"
        )
        conn.execute("alter table schema_migrations enable row level security")
