"""Job `backfill-values`: serie diaria de valor (un año) e historial de traspasos
en la liga de cada jugador (/ajax/sw/players).

Una petición por jugador. Universo: jugadores con minutos esta temporada, en
alguna plantilla de la liga o en el mercado. Reanudable: salta a quien ya tenga
su ficha pedida. Tras la carga inicial, el valor diario lo añade
snapshot-daily para los jugadores con dueño y los del mercado.
"""

from __future__ import annotations

from datetime import date
from functools import partial
from typing import Any

import psycopg

from mister_assistant.jobs.common import DatabaseLostError, JobResult, StepRunner, today_madrid
from mister_assistant.sources.mister import MisterClient, SessionExpiredError
from mister_assistant.store import normalize as nz
from mister_assistant.store import repo

JOB = "backfill_values"
Conn = psycopg.Connection[tuple[Any, ...]]


def universe(conn: Conn) -> list[int]:
    rows = conn.execute(
        "select player_id from ("
        "  select player_id from player_gameweek where minutes > 0"
        "  union select player_id from squad_snapshot"
        "   where snapshot_date = (select max(snapshot_date) from squad_snapshot)"
        "  union select player_id from market_snapshot"
        "   where snapshot_date = (select max(snapshot_date) from market_snapshot)"
        "  union select player_id from league_events where player_id is not null"
        # Cargado = ya se pidió su ficha (los fichajes recientes tienen menos de un
        # año de serie, así que contar días los volvería a pedir siempre).
        ") u where player_id not in ("
        "  select (params->>'id')::int from raw_responses"
        "  where source = 'mister' and route = 'player')"
        " order by player_id",
    ).fetchall()
    return [int(r[0]) for r in rows]


def run_backfill_values(
    conn: Conn, client: MisterClient, *, max_requests: int | None = None,
    run_date: date | None = None,
) -> JobResult:  # fmt: skip
    run_date = run_date or today_madrid()
    result = JobResult(JOB, run_date)
    run_id = repo.start_job(conn, JOB, run_date)
    steps = StepRunner(conn, result)
    result.stats = {"jugadores": 0, "días": 0}
    try:
        todo = universe(conn)
        result.stats["pendientes"] = len(todo)
        for pid in todo:
            if max_requests is not None and client.requests >= max_requests:
                result.errors.append(f"límite de {max_requests} peticiones; vuelve a ejecutar")
                break
            steps.run(f"jugador {pid}", partial(_player, conn, client, pid, run_date, result))
        result.stats["pendientes"] = len(universe(conn))
        result.status = steps.final_status()
        if result.status == "ok" and result.stats["pendientes"]:
            result.status = "partial"
    except SessionExpiredError as exc:
        result.status = "failed"
        result.session_expired = True
        result.errors.append(f"sesión caducada: {exc}")
    except DatabaseLostError as exc:
        steps.abort_on_db_loss(exc)
    finally:
        result.requests = client.requests
        result.close(conn, run_id)
    return result


def _player(conn: Conn, client: MisterClient, pid: int, run_date: date, result: JobResult) -> None:
    resp = client.player(pid, "")
    repo.save_raw(conn, resp, run_date)
    rows = nz.value_history(resp.data)
    repo.insert_rows(conn, "player_value_daily", rows, ["player_id", "value_date"], update=True)
    # El historial de traspasos de la ficha llega hasta el inicio de temporada.
    events = nz.owner_events(resp.data, _community_id(conn))
    repo.insert_rows(conn, "league_events", events, ["event_key"], json_columns=["payload"])
    result.stats["jugadores"] += 1
    result.stats["días"] += len(rows)
    result.stats["traspasos"] = result.stats.get("traspasos", 0) + len(events)


def _community_id(conn: Conn) -> int:
    row = conn.execute("select community_id from managers where is_me limit 1").fetchone()
    return int(row[0]) if row else 0
