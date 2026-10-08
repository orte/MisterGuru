"""Job `capture-lineups`: alineaciones probables de Mister y Fútbol Fantasy.

Se ejecuta varias veces por semana: la evolución de la probabilidad es un dato
en sí. Cada captura añade filas a lineup_forecast (append-only) y empareja los
jugadores externos nuevos. Peticiones: 1 a Mister (jornada actual) y 11 a Fútbol
Fantasy (índice + 10 partidos). Cada partido falla por separado.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any

import psycopg

from mister_assistant.identity import store as idstore
from mister_assistant.identity.matching import ExternalPlayer
from mister_assistant.jobs.common import DatabaseLostError, JobResult, StepRunner, today_madrid
from mister_assistant.sources import futbolfantasy as ff
from mister_assistant.sources.http import PoliteClient
from mister_assistant.sources.mister import MisterClient, SessionExpiredError
from mister_assistant.store import normalize as nz
from mister_assistant.store import repo

log = logging.getLogger(__name__)

JOB = "capture_lineups"
Conn = psycopg.Connection[tuple[Any, ...]]


@dataclass(frozen=True)
class LineupRow(nz.Row):
    source: str
    external_id: str
    gameweek_id: int
    captured_at: datetime
    fixture_id: int | None
    team_id: int | None
    player_name: str | None
    probability: float | None
    role: str | None
    injury_code: int | None
    confirmed: bool


@dataclass(frozen=True)
class Fixture:
    fixture_id: int
    gameweek_id: int
    number: int


# Un partido de Fútbol Fantasy es el fixture de Mister con esos equipos que se
# juega en esta ventana (cubre también aplazados de jornadas anteriores).
FIXTURE_WINDOW = timedelta(days=10)


def run_capture_lineups(
    conn: Conn,
    mister: MisterClient | None,
    ff_client: PoliteClient | None,
    *,
    run_date: date | None = None,
    now: datetime | None = None,
) -> JobResult:
    run_date = run_date or today_madrid()
    captured_at = now or datetime.now(UTC)
    result = JobResult(JOB, run_date)
    run_id = repo.start_job(conn, JOB, run_date)
    steps = StepRunner(conn, result)
    try:
        if mister is not None:
            steps.run("mister", partial(_mister, conn, mister, run_date, captured_at, result))
        if ff_client is not None:
            upcoming = _upcoming_fixtures(conn, captured_at)
            if not upcoming:
                raise ValueError("no hay partidos próximos en la BD: ejecuta antes snapshot-daily")
            matches = steps.run(
                "ff índice", partial(_ff_index, conn, ff_client, run_date, captured_at)
            )
            for m, home, away in matches or []:
                steps.run(
                    f"ff partido {m.match_id}",
                    partial(
                        _ff_match, conn, ff_client, m, home, away, upcoming, run_date,
                        captured_at, result,
                    ),
                )  # fmt: skip
        result.status = steps.final_status()
    except SessionExpiredError as exc:
        result.status = "failed"
        result.session_expired = True
        result.errors.append(f"sesión caducada: {exc}")
    except DatabaseLostError as exc:
        steps.abort_on_db_loss(exc)
    except ValueError as exc:
        result.status = "failed"
        result.errors.append(str(exc))
    finally:
        result.requests = (mister.requests if mister else 0) + (
            ff_client.requests if ff_client else 0
        )
        result.close(conn, run_id)
    return result


def _upcoming_fixtures(conn: Conn, now: datetime) -> dict[tuple[int, int], Fixture]:
    rows = conn.execute(
        "select f.id, f.gameweek_id, g.number, f.home_team_id, f.away_team_id"
        " from fixtures f join gameweeks g on g.id = f.gameweek_id"
        " where f.status is distinct from 'played'"
        "  and f.kickoff_at between %s and %s",
        (now - timedelta(days=1), now + FIXTURE_WINDOW),
    ).fetchall()
    return {(int(r[3]), int(r[4])): Fixture(int(r[0]), int(r[1]), int(r[2])) for r in rows}


def _mister(
    conn: Conn, client: MisterClient, run_date: date, captured_at: datetime, result: JobResult
) -> None:
    """Once probable de Mister (`preview`) de la jornada actual; guarda sus partidos."""
    resp = client.gameweek()
    repo.save_raw(conn, resp, run_date)
    data = resp.data
    gameweek_id = int(data["gameweekStatus"]["id"])
    repo.insert_rows(conn, "gameweeks", nz.gameweeks_from(data), ["id"], update=True)
    repo.insert_rows(conn, "fixtures", nz.fixtures_from(data), ["id"], update=True)
    rows: list[LineupRow] = []
    players: list[nz.PlayerRow] = []
    for match_id, match in (data.get("preview") or {}).items():
        confirmed = bool(match.get("confirmed"))
        for team_id, plist in (match.get("players") or {}).items():
            for p in plist:
                players.append(
                    nz.PlayerRow(
                        int(p["id"]),
                        str(p["name"]),
                        p.get("short"),
                        None,
                        p.get("position"),
                        int(team_id),
                    )
                )
                rows.append(
                    LineupRow(
                        source="mister",
                        external_id=str(p["id"]),
                        gameweek_id=gameweek_id,
                        captured_at=captured_at,
                        fixture_id=int(match_id),
                        team_id=int(team_id),
                        player_name=str(p["name"]),
                        probability=None,
                        role="titular",
                        injury_code=None,
                        confirmed=confirmed or bool(p.get("confirmed")),
                    )
                )
    repo.upsert_players(conn, players)
    _insert_lineups(conn, rows)
    result.stats["mister"] = len(rows)


def _ff_index(
    conn: Conn, client: PoliteClient, run_date: date, captured_at: datetime
) -> list[tuple[ff.FFMatch, int, int]]:
    """Partidos de la jornada en FF, con sus equipos ya emparejados (y guardados)."""
    resp = client.get(ff.INDEX_PATH)
    matches = ff.parse_index(resp.text)
    teams = idstore.load_teams(conn)
    mapped = [
        (
            m,
            idstore.team_id_for(conn, ff.SOURCE, m.home_team_id, m.home_name, teams),
            idstore.team_id_for(conn, ff.SOURCE, m.away_team_id, m.away_name, teams),
        )
        for m in matches
    ]
    repo.save_raw_payload(
        conn,
        source=ff.SOURCE,
        route="index",
        params={},
        run_date=run_date,
        captured_at=captured_at,
        body_json=[m.__dict__ for m in matches],
    )
    return mapped


def _ff_match(
    conn: Conn,
    client: PoliteClient,
    m: ff.FFMatch,
    home: int,
    away: int,
    upcoming: dict[tuple[int, int], Fixture],
    run_date: date,
    captured_at: datetime,
    result: JobResult,
) -> None:
    fixture = upcoming.get((home, away))
    if fixture is None:
        raise ValueError(f"{m.home_name}-{m.away_name}: sin partido próximo en Mister")
    if m.jornada is not None and m.jornada != fixture.number:
        log.info(
            "%s-%s: J%s en Fútbol Fantasy, J%s en Mister (aplazado)",
            m.home_name, m.away_name, m.jornada, fixture.number,
        )  # fmt: skip

    resp = client.get(m.path)
    html = resp.text
    repo.save_raw_payload(
        conn,
        source=ff.SOURCE,
        route="match",
        params={"match_id": m.match_id},
        run_date=run_date,
        captured_at=captured_at,
        body_json=ff.raw_players(html),
    )
    players = ff.parse_match(html)
    rows = [
        LineupRow(
            source=ff.SOURCE,
            external_id=p.player_id,
            gameweek_id=fixture.gameweek_id,
            captured_at=captured_at,
            fixture_id=fixture.fixture_id,
            team_id=home if p.side == "local" else away,
            player_name=p.name,
            probability=p.probability,
            role=p.role,
            injury_code=p.injury_code,
            confirmed=False,
        )
        for p in players
    ]
    _insert_lineups(conn, rows)
    externals = [
        ExternalPlayer(
            external_id=p.player_id,
            name=p.name,
            slug=p.slug,
            team_id=home if p.side == "local" else away,
            goalkeeper=p.goalkeeper,
        )
        for p in players
    ]
    stats = idstore.match_externals(conn, ff.SOURCE, externals, idstore.load_candidates(conn))
    result.stats["ff jugadores"] = result.stats.get("ff jugadores", 0) + len(rows)
    result.stats["ff emparejados nuevos"] = (
        result.stats.get("ff emparejados nuevos", 0) + stats.accepted
    )
    result.stats["ff a revisar"] = (
        result.stats.get("ff a revisar", 0) + stats.review + stats.unmatched
    )


def _insert_lineups(conn: Conn, rows: list[LineupRow]) -> None:
    unique = {(r.source, r.external_id, r.gameweek_id): r for r in rows}
    repo.insert_rows(
        conn,
        "lineup_forecast",
        list(unique.values()),
        ["source", "external_id", "gameweek_id", "captured_at"],
    )
