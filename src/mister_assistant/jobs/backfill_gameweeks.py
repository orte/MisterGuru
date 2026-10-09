"""Job `backfill-gameweeks`: desglose por fuente de las jornadas ya disputadas.

Por cada jornada cerrada o en curso: una petición a /ajax/sw/gameweek (partidos,
quién jugó y sus eventos) y una a /ajax/player-gameweek por cada jugador que jugó
un partido ya puntuado (~300 por jornada). Es reanudable: salta los jugadores ya guardados, así que
puede cortarse con `max_requests` y seguir otro día. Tras el backfill inicial,
la ejecución diaria solo pide las jornadas recién cerradas.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date
from functools import partial
from typing import Any

import psycopg

from mister_assistant.jobs.common import DatabaseLostError, JobResult, StepRunner, today_madrid
from mister_assistant.sources.mister import MisterClient, SessionExpiredError
from mister_assistant.store import normalize as nz
from mister_assistant.store import repo

log = logging.getLogger(__name__)

JOB = "backfill_gameweeks"
Conn = psycopg.Connection[tuple[Any, ...]]


def run_backfill(
    conn: Conn,
    client: MisterClient,
    *,
    max_requests: int | None = None,
    numbers: Iterable[int] | None = None,
    run_date: date | None = None,
) -> JobResult:
    run_date = run_date or today_madrid()
    result = JobResult(JOB, run_date)
    run_id = repo.start_job(conn, JOB, run_date)
    steps = StepRunner(conn, result)
    wanted = set(numbers) if numbers is not None else None
    result.stats = {"jornadas": 0, "desgloses nuevos": 0, "pendientes": 0}

    def budget_left() -> bool:
        return max_requests is None or client.requests < max_requests

    try:
        current = client.gameweek()
        me_id = int(current.data["id_manager"])
        with conn.transaction():
            repo.save_raw(conn, current, run_date)
            repo.insert_rows(
                conn, "gameweeks", nz.gameweeks_from(current.data), ["id"], update=True
            )
        loaded = fully_loaded_gameweeks(conn)
        finished = [
            g
            for g in nz.gameweeks_from(current.data)
            # `ongoing` = jornada con partidos ya jugados y alguno pendiente (aplazado):
            # se vuelve a mirar hasta que cierre. Una `finished` ya cargada se salta
            # (salvo que se pida su número), para no pedir su página cada día.
            if g.status in ("finished", "ongoing")
            and (wanted is None or g.number in wanted)
            and (g.status == "ongoing" or g.id not in loaded or wanted is not None)
        ]
        for gw in finished:
            if not budget_left():
                break
            played = steps.run(
                f"jornada {gw.number}", partial(_gameweek, conn, client, gw.id, run_date)
            )
            if played is None:
                continue
            result.stats["jornadas"] += 1
            done = repo.existing_player_gameweeks(conn, gw.id)
            todo = [p for p in played if p.player.mister_player_id not in done]
            for p in todo:
                if not budget_left():
                    break
                ok = steps.run(
                    f"jugador {p.player.mister_player_id} J{gw.number}",
                    partial(_player, conn, client, me_id, p, run_date),
                )
                if ok:
                    result.stats["desgloses nuevos"] += 1
            remaining = len(repo.existing_player_gameweeks(conn, gw.id))
            result.stats["pendientes"] += max(0, len(played) - remaining)
        result.status = steps.final_status()
        if result.status == "ok" and not budget_left():
            result.status = "partial"
            result.errors.append(
                f"límite de {max_requests} peticiones alcanzado; vuelve a ejecutar para seguir"
            )
    except SessionExpiredError as exc:
        result.status = "failed"
        result.session_expired = True
        result.errors.append(f"sesión caducada: {exc}")
    except DatabaseLostError as exc:
        steps.abort_on_db_loss(exc)
    except psycopg.OperationalError as exc:
        # Accesos a la BD fuera de un paso (p. ej. qué falta por pedir).
        if not (conn.closed or conn.broken):
            raise
        steps.abort_on_db_loss(DatabaseLostError(str(exc).splitlines()[0]))
    finally:
        result.requests = client.requests
        result.close(conn, run_id)
    return result


# Jugadores con desglose por partido para dar una jornada por completa (~30 de media).
MIN_PLAYERS_PER_MATCH = 20


def fully_loaded_gameweeks(conn: Conn) -> set[int]:
    """Jornadas en las que cada partido jugado tiene ya sus desgloses.

    Una carga cortada a mitad deja partidos con pocos o ningún jugador: esas
    jornadas no cuentan como cargadas y se vuelven a pedir.
    """
    rows = conn.execute(
        "select f.gameweek_id, bool_and(coalesce(n.players, 0) >= %s)"
        " from fixtures f left join ("
        "  select match_id, count(*) as players from player_gameweek group by match_id"
        " ) n on n.match_id = f.id"
        " where f.status = 'played' group by f.gameweek_id",
        (MIN_PLAYERS_PER_MATCH,),
    ).fetchall()
    return {int(gid) for gid, complete in rows if complete}


def _gameweek(
    conn: Conn, client: MisterClient, gameweek_id: int, run_date: date
) -> list[nz.PlayedPlayer]:
    resp = client.gameweek(gameweek_id)
    repo.save_raw(conn, resp, run_date)
    data = resp.data
    repo.upsert_teams(conn, nz.teams_from_fixtures(data))
    repo.insert_rows(conn, "fixtures", nz.fixtures_from(data), ["id"], update=True)
    played = nz.played_players_from(data)
    repo.upsert_players(conn, [p.player for p in played])
    return played


def _player(
    conn: Conn, client: MisterClient, me_id: int, p: nz.PlayedPlayer, run_date: date
) -> bool:
    resp = client.player_gameweek(me_id, p.gameweek_id, p.player.mister_player_id)
    repo.save_raw(conn, resp, run_date)
    row = nz.player_gameweek_row(resp.data, p.events)
    repo.insert_rows(conn, "player_gameweek", [row], ["player_id", "gameweek_id"], update=True)
    stats = nz.match_stats_row(resp.data)
    if stats is not None:
        repo.insert_rows(conn, "match_stats", [stats], ["player_id", "fixture_id"], update=True)
    return True
