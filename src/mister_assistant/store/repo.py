"""Escritura en Postgres.

- Snapshots y eventos: `on conflict do nothing` → idempotente y append-only.
- Dimensiones (players, managers, teams, gameweeks, fixtures) y player_gameweek:
  upsert con el último valor conocido (Mister corrige notas a mano a veces).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from mister_assistant.sources.mister import MisterResponse
from mister_assistant.store.normalize import Row

Conn = psycopg.Connection[tuple[Any, ...]]

# Claves que nunca deben guardarse en crudo (credenciales o ruido).
_STRIP_KEYS = frozenset({"auth", "api_key", "email", "phone", "ads"})


def sanitize(obj: Any) -> Any:
    """Copia de la respuesta sin secretos (p. ej. `cfg.auth`, el x-auth vigente)."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if k not in _STRIP_KEYS}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def params_key(params: dict[str, str] | Any) -> str:
    canonical = json.dumps(dict(params), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def save_raw(conn: Conn, resp: MisterResponse, run_date: date, source: str = "mister") -> None:
    body_json = Jsonb(sanitize(resp.payload)) if resp.payload is not None else None
    body_text = None if resp.payload is not None else resp.text
    conn.execute(
        "insert into raw_responses"
        " (source, route, params, params_key, run_date, captured_at, status_code,"
        "  body_json, body_text)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " on conflict (source, route, params_key, run_date) do nothing",
        (
            source,
            resp.route,
            Jsonb(dict(resp.params)),
            params_key(resp.params),
            run_date,
            resp.fetched_at,
            resp.status_code,
            body_json,
            body_text,
        ),
    )


def insert_rows(
    conn: Conn,
    table: str,
    rows: Sequence[Row],
    conflict: Sequence[str],
    *,
    update: bool = False,
    json_columns: Iterable[str] = (),
) -> int:
    """Inserta filas; con `update=True` actualiza las existentes (upsert)."""
    if not rows:
        return 0
    dicts = [r.as_dict() for r in rows]
    cols = list(dicts[0])
    jcols = set(json_columns)
    values = [
        tuple(
            Jsonb(json.loads(d[c])) if c in jcols and isinstance(d[c], str) else d[c] for c in cols
        )
        for d in dicts
    ]
    stmt = sql.SQL("insert into {} ({}) values ({}) on conflict ({}) ").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols)),
        sql.SQL(", ").join(map(sql.Identifier, conflict)),
    )
    updatable = [c for c in cols if c not in conflict]
    if update and updatable:
        sets: list[sql.Composable] = [
            sql.SQL("{0} = excluded.{0}").format(sql.Identifier(c)) for c in updatable
        ]
        has_updated_at = table in _TABLES_WITH_UPDATED_AT
        if has_updated_at:
            sets.append(sql.SQL("updated_at = now()"))
        if table == "player_gameweek":
            sets.append(sql.SQL("captured_at = now()"))
        stmt += sql.SQL("do update set ") + sql.SQL(", ").join(sets)
    else:
        stmt += sql.SQL("do nothing")
    with conn.cursor() as cur:
        cur.executemany(stmt, values)
        return cur.rowcount if cur.rowcount >= 0 else len(values)


_TABLES_WITH_UPDATED_AT = frozenset({"players", "managers", "teams", "gameweeks", "fixtures"})


def upsert_players(conn: Conn, rows: Sequence[Row]) -> None:
    """Upsert de jugadores sin pisar slug/short_name conocidos con nulos."""
    if not rows:
        return
    unique = {r.as_dict()["mister_player_id"]: r for r in rows}
    with conn.cursor() as cur:
        cur.executemany(
            "insert into players (mister_player_id, name, short_name, slug, position, team_id)"
            " values (%(mister_player_id)s, %(name)s, %(short_name)s, %(slug)s,"
            "         %(position)s, %(team_id)s)"
            " on conflict (mister_player_id) do update set"
            "  name = case when length(excluded.name) > length(players.name)"
            "              then excluded.name else players.name end,"
            "  short_name = coalesce(excluded.short_name, players.short_name),"
            "  slug = coalesce(excluded.slug, players.slug),"
            "  position = coalesce(excluded.position, players.position),"
            "  team_id = coalesce(excluded.team_id, players.team_id),"
            "  updated_at = now()",
            [r.as_dict() for r in unique.values()],
        )


def upsert_teams(conn: Conn, teams: dict[int, str]) -> None:
    if not teams:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "insert into teams (id, name) values (%s, %s)"
            " on conflict (id) do update set name = excluded.name, updated_at = now()",
            sorted(teams.items()),
        )


def start_job(conn: Conn, job: str, run_date: date) -> int:
    row = conn.execute(
        "insert into job_runs (job, run_date) values (%s, %s) returning id", (job, run_date)
    ).fetchone()
    assert row is not None
    return int(row[0])


def finish_job(conn: Conn, run_id: int, status: str, requests: int, errors: list[str]) -> None:
    conn.execute(
        "update job_runs set finished_at = now(), status = %s, requests = %s, errors = %s"
        " where id = %s",
        (status, requests, Jsonb(errors), run_id),
    )


def job_succeeded(conn: Conn, job: str, run_date: date) -> bool:
    row = conn.execute(
        "select 1 from job_runs where job = %s and run_date = %s and status = 'ok' limit 1",
        (job, run_date),
    ).fetchone()
    return row is not None


def existing_player_gameweeks(conn: Conn, gameweek_id: int) -> set[int]:
    rows = conn.execute(
        "select player_id from player_gameweek where gameweek_id = %s", (gameweek_id,)
    ).fetchall()
    return {int(r[0]) for r in rows}


def existing_event_keys(conn: Conn, keys: Sequence[str]) -> set[str]:
    if not keys:
        return set()
    rows = conn.execute(
        "select event_key from league_events where event_key = any(%s)", (list(keys),)
    ).fetchall()
    return {str(r[0]) for r in rows}
