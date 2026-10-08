"""Job `snapshot-daily`: foto diaria de la liga, cruda y normalizada.

Idempotente por día: si ya hay una ejecución `ok` para hoy no hace nada (salvo
`force`), y las filas de snapshot usan `on conflict do nothing`.
Unas 20 peticiones: jornada, clasificación, saldo, un /ajax/sw/users por
mánager, mercado y de 1 a 5 páginas de feed.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date
from functools import partial
from typing import Any

import psycopg

from mister_assistant.jobs.common import DatabaseLostError, JobResult, StepRunner, today_madrid
from mister_assistant.sources.mister import MisterClient, SessionExpiredError
from mister_assistant.sources.mister_html import StandingRow, parse_market, parse_standings
from mister_assistant.sources.mister_models import Balance
from mister_assistant.store import normalize as nz
from mister_assistant.store import repo

log = logging.getLogger(__name__)

JOB = "snapshot_daily"
FEED_MAX_PAGES = 5
FEED_PAGE_SIZE = 20

Conn = psycopg.Connection[tuple[Any, ...]]


def run_snapshot_daily(
    conn: Conn, client: MisterClient, *, run_date: date | None = None, force: bool = False
) -> JobResult:
    run_date = run_date or today_madrid()
    result = JobResult(JOB, run_date)
    if not force and repo.job_succeeded(conn, JOB, run_date):
        result.status = "skipped"
        return result

    run_id = repo.start_job(conn, JOB, run_date)
    steps = StepRunner(conn, result)
    try:
        me_id = steps.run("jornada", lambda: _gameweek(conn, client, run_date, result))
        standings = steps.run("clasificación", lambda: _standings(conn, client, run_date)) or []
        balance = steps.run("saldo", lambda: _balance(conn, client, run_date))
        community_id: int | None = None
        # Un mánager sin plantilla (abandonó la liga) no tiene «N jugadores · €» en la
        # clasificación y su /ajax/sw/users da 404: se guarda solo desde la clasificación.
        for row in (r for r in standings if r.squad_size is not None):
            community_id = (
                steps.run(
                    f"plantilla {row.manager_id}",
                    partial(_user, conn, client, run_date, row, me_id, balance, result),
                )
                or community_id
            )
        for row in (r for r in standings if r.squad_size is None):
            steps.run(
                f"mánager sin plantilla {row.manager_id}",
                partial(_standing_only, conn, run_date, row, community_id, result),
            )
        steps.run("mercado", lambda: _market(conn, client, run_date, result))
        steps.run("feed", lambda: _feed(conn, client, run_date, result))
        result.status = steps.final_status()
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


def _gameweek(conn: Conn, client: MisterClient, run_date: date, result: JobResult) -> int:
    resp = client.gameweek()
    repo.save_raw(conn, resp, run_date)
    data = resp.data
    repo.insert_rows(conn, "gameweeks", nz.gameweeks_from(data), ["id"], update=True)
    repo.upsert_teams(conn, nz.teams_from_fixtures(data))
    fixtures = nz.fixtures_from(data)
    repo.insert_rows(conn, "fixtures", fixtures, ["id"], update=True)
    result.stats["partidos"] = len(fixtures)
    # Partidos de la jornada siguiente: el informe de la víspera los necesita aunque
    # la actual aún no haya terminado (jornadas entre semana) y las cuotas también.
    current_id = int(data["gameweekStatus"]["id"])
    upcoming = [
        g for g in nz.gameweeks_from(data) if g.status == "unstarted" and g.id != current_id
    ]
    if upcoming:
        nxt = min(upcoming, key=lambda g: g.number)
        nresp = client.gameweek(nxt.id)
        repo.save_raw(conn, nresp, run_date)
        repo.insert_rows(conn, "fixtures", nz.fixtures_from(nresp.data), ["id"], update=True)
        repo.upsert_teams(conn, nz.teams_from_fixtures(nresp.data))
    return int(data["id_manager"])


def _standings(conn: Conn, client: MisterClient, run_date: date) -> list[StandingRow]:
    resp = client.page("standings")
    repo.save_raw(conn, resp, run_date)
    return parse_standings(resp.text)


def _balance(conn: Conn, client: MisterClient, run_date: date) -> Balance:
    resp = client.call("balance")
    repo.save_raw(conn, resp, run_date)
    return Balance.model_validate(resp.data)


def _user(
    conn: Conn,
    client: MisterClient,
    run_date: date,
    row: StandingRow,
    me_id: int | None,
    balance: Balance | None,
    result: JobResult,
) -> int:
    resp = client.user(row.manager_id, row.slug)
    repo.save_raw(conn, resp, run_date)
    is_me = row.is_me or row.manager_id == me_id
    snap = nz.user_snapshot(
        resp.data, snapshot_date=run_date, slug=row.slug, is_me=is_me, standing=row
    )
    manager_snap = snap.snapshot
    if is_me and balance is not None:
        manager_snap = replace(
            manager_snap,
            balance=balance.current,
            balance_future=balance.future,
            max_bid=balance.max_debt,
        )
    repo.insert_rows(conn, "managers", [snap.manager], ["mister_manager_id"], update=True)
    repo.upsert_players(conn, snap.players)
    repo.insert_rows(conn, "manager_snapshot", [manager_snap], ["manager_id", "snapshot_date"])
    repo.insert_rows(conn, "squad_snapshot", snap.squad, ["player_id", "snapshot_date"])
    result.stats["mánagers"] = result.stats.get("mánagers", 0) + 1
    result.stats["jugadores en plantillas"] = result.stats.get("jugadores en plantillas", 0) + len(
        snap.squad
    )
    return snap.manager.community_id


def _standing_only(
    conn: Conn, run_date: date, row: StandingRow, community_id: int | None, result: JobResult
) -> None:
    if community_id is None:
        raise ValueError("sin id de liga: no se ha podido leer ninguna plantilla")
    manager = nz.ManagerRow(
        mister_manager_id=row.manager_id,
        community_id=community_id,
        name=row.name,
        slug=row.slug,
        is_me=row.is_me,
    )
    snapshot = nz.ManagerSnapshotRow(
        manager_id=row.manager_id,
        snapshot_date=run_date,
        season_rank=row.rank,
        season_points=row.points,
        team_value=None,
        team_value_prev=None,
        squad_size=0,
    )
    repo.insert_rows(conn, "managers", [manager], ["mister_manager_id"], update=True)
    repo.insert_rows(conn, "manager_snapshot", [snapshot], ["manager_id", "snapshot_date"])
    result.stats["mánagers sin plantilla"] = result.stats.get("mánagers sin plantilla", 0) + 1


def _market(conn: Conn, client: MisterClient, run_date: date, result: JobResult) -> None:
    resp = client.page("market")
    repo.save_raw(conn, resp, run_date)
    rows = parse_market(resp.text)
    repo.upsert_players(conn, nz.market_players(rows))
    repo.insert_rows(
        conn, "market_snapshot", nz.market_rows(rows, run_date), ["player_id", "snapshot_date"]
    )
    result.stats["en mercado"] = len(rows)


def _feed(conn: Conn, client: MisterClient, run_date: date, result: JobResult) -> None:
    """Pagina el feed hasta encontrar una página sin eventos nuevos."""
    new_total = 0
    for page in range(FEED_MAX_PAGES):
        resp = client.call(
            "feed",
            offset=page * FEED_PAGE_SIZE,
            cardsPerPage=FEED_PAGE_SIZE,
            end=False,
            loading=True,
        )
        repo.save_raw(conn, resp, run_date)
        cards = resp.data if isinstance(resp.data, list) else []
        events = nz.league_events_from(cards)
        known = repo.existing_event_keys(conn, [e.event_key for e in events])
        new = [e for e in events if e.event_key not in known]
        repo.insert_rows(conn, "league_events", new, ["event_key"], json_columns=["payload"])
        new_total += len(new)
        if not cards or len(cards) < FEED_PAGE_SIZE or (events and not new):
            break
    result.stats["eventos nuevos"] = new_total
