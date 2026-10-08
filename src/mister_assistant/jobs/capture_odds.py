"""Job `capture-odds`: cuotas 1X2 y más/menos goles (The Odds API).

Una petición por ejecución (2 créditos de 500 al mes). Guarda lo crudo y el
consenso sin margen por partido, emparejado con los fixtures de Mister.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any

import psycopg

from mister_assistant.identity import store as idstore
from mister_assistant.jobs.common import DatabaseLostError, JobResult, StepRunner, today_madrid
from mister_assistant.sources import odds as od
from mister_assistant.store import repo

JOB = "capture_odds"
Conn = psycopg.Connection[tuple[Any, ...]]
# Un partido de Mister y un evento de la API son el mismo si los equipos coinciden
# y el inicio difiere menos de esto (cambios de horario).
KICKOFF_TOLERANCE = timedelta(hours=36)


def run_capture_odds(
    conn: Conn, client: od.OddsClient, *, run_date: date | None = None, now: datetime | None = None
) -> JobResult:
    run_date = run_date or today_madrid()
    captured_at = now or datetime.now(UTC)
    result = JobResult(JOB, run_date)
    run_id = repo.start_job(conn, JOB, run_date)
    steps = StepRunner(conn, result)
    try:
        steps.run("cuotas", partial(_capture, conn, client, run_date, captured_at, result))
        result.status = steps.final_status()
    except DatabaseLostError as exc:
        steps.abort_on_db_loss(exc)
    finally:
        result.requests = client.requests
        result.close(conn, run_id)
    return result


def _capture(
    conn: Conn, client: od.OddsClient, run_date: date, captured_at: datetime, result: JobResult
) -> None:
    raw = client.odds()
    repo.save_raw_payload(
        conn,
        source=od.SOURCE,
        route="odds",
        params={"sport": od.SPORT},
        run_date=run_date,
        captured_at=captured_at,
        body_json=raw,
    )
    teams = idstore.load_teams(conn)
    fixtures = conn.execute(
        "select id, home_team_id, away_team_id, kickoff_at from fixtures"
        " where kickoff_at > now() - interval '1 day'"
    ).fetchall()
    rows: list[tuple[Any, ...]] = []
    unmatched = 0
    for ev in od.consensus(raw):
        home = idstore.team_id_for(conn, od.SOURCE, ev.home_name, ev.home_name, teams)
        away = idstore.team_id_for(conn, od.SOURCE, ev.away_name, ev.away_name, teams)
        fixture_id = next(
            (
                int(f[0])
                for f in fixtures
                if f[1] == home
                and f[2] == away
                and (f[3] is None or abs(f[3] - ev.commence_at) <= KICKOFF_TOLERANCE)
            ),
            None,
        )
        unmatched += fixture_id is None
        rows.extend(
            (
                ev.event_id,
                captured_at,
                fixture_id,
                home,
                away,
                ev.commence_at,
                ln.market,
                ln.outcome,
                ln.point,
                ln.price_avg,
                ln.prob_fair,
                ln.bookmakers,
            )
            for ln in ev.lines
        )
    with conn.cursor() as cur:
        cur.executemany(
            "insert into odds (event_id, captured_at, fixture_id, home_team_id, away_team_id,"
            "  commence_at, market, outcome, point, price_avg, prob_fair, bookmakers)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
            rows,
        )
    result.stats["líneas"] = len(rows)
    result.stats["partidos sin fixture"] = unmatched
    if client.quota.remaining is not None:
        result.stats["créditos restantes"] = client.quota.remaining
