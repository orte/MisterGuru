"""Integración Fase 2: alineaciones, cuotas, match_stats e identidades contra Postgres."""

from __future__ import annotations

import copy
import csv
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
from conftest import load_fixture
from test_odds import EVENT

from mister_assistant.config import Settings
from mister_assistant.identity import store as idstore
from mister_assistant.jobs.capture_lineups import run_capture_lineups
from mister_assistant.jobs.capture_odds import run_capture_odds
from mister_assistant.jobs.derive_match_stats import run_derive_match_stats
from mister_assistant.sources import futbolfantasy as ff
from mister_assistant.sources.http import PoliteClient
from mister_assistant.sources.mister import MisterClient
from mister_assistant.sources.odds import OddsClient
from mister_assistant.store import repo

FF_FIX = Path(__file__).parent / "fixtures" / "futbolfantasy"
DAY = date(2026, 10, 8)
NOW = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)
Conn = psycopg.Connection[tuple[Any, ...]]

TEAMS = {
    1: "Athletic Club", 2: "Atlético", 3: "Barcelona", 4: "Betis", 5: "Celta",
    6: "Deportivo da Coruña", 8: "Espanyol", 9: "Getafe", 12: "Levante", 13: "Málaga",
    14: "Rayo Vallecano", 15: "Real Madrid", 16: "Real Sociedad", 17: "Sevilla",
    19: "Valencia", 20: "Villarreal", 23: "Elche", 48: "Alavés", 50: "Osasuna",
    1490: "Racing de Santander",
}  # fmt: skip

# Jugadores de Mister de Betis (4) y Osasuna (50) que aparecen en la ficha de FF.
MISTER_PLAYERS = [
    (101, "Marc Roca", "M. Roca", 3, 4), (102, "Isco Alarcón", "Isco", 3, 4),
    (103, "Juan Camilo Hernández", "Cucho Hernández", 4, 4),
    (104, "Marc Bartra", "M. Bartra", 2, 4),
    (105, "Valentín Gómez", "V. Gómez", 2, 4), (201, "Alejandro Catena", "A. Catena", 2, 50),
    (202, "Aimar Oroz", "A. Oroz", 3, 50), (203, "Lucas Torró", "L. Torró", 3, 50),
    (204, "Raúl Moro", "R. Moro", 4, 50),
]  # fmt: skip


def current_gameweek() -> dict[str, Any]:
    """Jornada 8 sin empezar con Betis-Osasuna (38001) y su once probable en Mister."""
    gw: dict[str, Any] = copy.deepcopy(load_fixture("mister/gameweek_finished.json"))
    d = gw["data"]
    d["gameweekStatus"] = {**d["gameweekStatus"], "id": 4049, "gameweek": 8, "status": "unstarted"}
    template = d["games"][0]
    d["games"] = [
        {**template, "id": 38001, "id_gameweek": 4049, "id_home": 4, "id_away": 50,
         "home": "Betis", "away": "Osasuna", "status": "fixture", "goals_home": "-",
         "goals_away": "-", "date": {"ts": 1791736200}, "mixtos_graded_date": None},
    ]  # fmt: skip
    d["players"] = []
    d["preview"] = {
        "38001": {
            "confirmed": 0,
            "players": {
                "4": [{"id": 101, "name": "Marc Roca", "short": "M. Roca", "position": 3,
                       "id_team": 4, "confirmed": 0}],
                "50": [{"id": 201, "name": "Alejandro Catena", "short": "A. Catena",
                        "position": 2, "id_team": 50, "confirmed": 0}],
            },
        }
    }  # fmt: skip
    return gw


def seed(db: Conn) -> None:
    with db.transaction():
        repo.upsert_teams(db, TEAMS)
        with db.cursor() as cur:
            cur.executemany(
                "insert into players (mister_player_id, name, short_name, position, team_id)"
                " values (%s, %s, %s, %s, %s)",
                MISTER_PLAYERS,
            )
            # Minutos esta temporada para el criterio de cobertura.
            cur.executemany(
                "insert into player_gameweek (player_id, gameweek_id, minutes)"
                " values (%s, 4048, 90)",
                [(p[0],) for p in MISTER_PLAYERS],
            )


def mister_client(settings: Settings) -> MisterClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ajax/sw/gameweek"
        return httpx.Response(200, json=current_gameweek())

    return MisterClient(settings, transport=httpx.MockTransport(handler), sleep=lambda _: None)


def ff_client(calls: list[str] | None = None) -> PoliteClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path == ff.INDEX_PATH:
            return httpx.Response(200, text=(FF_FIX / "index.html").read_text(encoding="utf-8"))
        if request.url.path == "/partidos/22499-betis-osasuna":
            return httpx.Response(
                200, text=(FF_FIX / "match_22499.html").read_text(encoding="utf-8")
            )
        return httpx.Response(404)

    return PoliteClient(
        ff.BASE_URL, min_interval_s=0, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )


def count(conn: Conn, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def test_capture_lineups_end_to_end(db: Conn, settings: Settings, tmp_path: Path) -> None:
    seed(db)
    calls: list[str] = []
    with mister_client(settings) as mister:
        result = run_capture_lineups(db, mister, ff_client(calls), run_date=DAY, now=NOW)

    # Los otros 9 partidos de FF no están en la jornada simulada de Mister: fallan
    # por separado y no se piden sus fichas.
    assert result.status == "partial"
    assert sum(1 for c in calls if c.startswith("/partidos/")) == 1
    assert count(db, "select count(*) from team_xref where source = 'futbolfantasy'") == 20

    assert count(db, "select count(*) from lineup_forecast where source = 'mister'") == 2
    ff_rows = db.execute(
        "select external_id, fixture_id, team_id, probability, role, injury_code"
        " from lineup_forecast where source = 'futbolfantasy' order by external_id"
    ).fetchall()
    assert len(ff_rows) == 14 and all(r[1] == 38001 for r in ff_rows)
    bartra = next(r for r in ff_rows if r[0] == "322")
    assert (bartra[2], float(bartra[3]), bartra[4], bartra[5]) == (4, 0.3, "suplente", 2)

    xref: dict[str, int] = dict(
        db.execute(
            "select external_id, player_id from player_xref where source = 'futbolfantasy'"
        ).fetchall()
    )
    assert xref["3215"] == 101  # Marc Roca, exacto
    assert xref["5546"] == 103  # Cucho Hernández ↔ nombre corto de Mister
    assert xref["7870"] == 204  # Raul Moro ↔ Raúl Moro
    # Los que no existen en Mister quedan en la cola.
    pending = count(db, "select count(*) from identity_review where status = 'pending'")
    assert pending == 14 - len(xref)

    cov = idstore.coverage(db, "futbolfantasy")
    assert cov.total == len(MISTER_PLAYERS) and cov.matched == len(MISTER_PLAYERS)

    # Revisión manual por CSV: se ignoran todos los pendientes.
    path = tmp_path / "review.csv"
    assert idstore.export_review(db, path) == pending
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    for r in rows:
        r["decision"] = "ignorar"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with db.transaction():
        stats = idstore.import_review(db, path)
    assert stats.ignored == pending
    assert count(db, "select count(*) from identity_review where status = 'pending'") == 0

    # Segunda captura: añade una serie temporal nueva, no re-empareja.
    later = NOW.replace(hour=18)
    with mister_client(settings) as mister:
        run_capture_lineups(db, mister, ff_client(), run_date=DAY, now=later)
    assert count(db, "select count(*) from lineup_forecast where source = 'futbolfantasy'") == 28
    assert count(db, "select count(*) from player_xref") == len(xref)


def test_capture_lineups_without_mister_uses_db_context(db: Conn, settings: Settings) -> None:
    seed(db)
    with mister_client(settings) as mister:
        run_capture_lineups(db, mister, None, run_date=DAY, now=NOW)
    result = run_capture_lineups(db, None, ff_client(), run_date=DAY, now=NOW.replace(hour=9))
    assert result.status == "partial"
    assert count(db, "select count(*) from lineup_forecast where source = 'futbolfantasy'") == 14


def test_capture_odds_maps_fixture(db: Conn, settings: Settings) -> None:
    seed(db)
    with mister_client(settings) as mister:
        run_capture_lineups(db, mister, None, run_date=DAY, now=NOW)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[EVENT], headers={"x-requests-remaining": "498"})

    with OddsClient("k", transport=httpx.MockTransport(handler)) as client:
        result = run_capture_odds(db, client, run_date=DAY, now=NOW)
    assert result.status == "ok", result.errors
    assert result.stats["partidos sin fixture"] == 0
    rows = db.execute(
        "select fixture_id, market, outcome, prob_fair from odds order by market, outcome"
    ).fetchall()
    assert {r[0] for r in rows} == {38001}
    assert sum(float(r[3]) for r in rows if r[1] == "h2h") == pytest_approx_one()
    assert count(db, "select count(*) from team_xref where source = 'the_odds_api'") == 2


def pytest_approx_one() -> Any:
    import pytest

    return pytest.approx(1.0, abs=1e-3)


def test_derive_match_stats_from_raw(db: Conn) -> None:
    pgw = load_fixture("mister/player_gameweek_4836153.json")
    with db.transaction():
        repo.save_raw_payload(
            db, source="mister", route="player_gameweek", params={"id_player": "4836153"},
            run_date=DAY, captured_at=NOW, body_json=pgw,
        )  # fmt: skip
    assert run_derive_match_stats(db) == 1
    row = db.execute("select player_id, fixture_id, minutes, xg, shots from match_stats").fetchone()
    assert row is not None and row[0] == 4836153 and row[1] == 38071
    assert row[2] == 62 and float(row[3]) > 0 and row[4] > 0
    # Idempotente.
    assert run_derive_match_stats(db) == 1
    assert count(db, "select count(*) from match_stats") == 1


def test_export_never_prefills_decision(db: Conn, settings: Settings, tmp_path: Path) -> None:
    seed(db)
    with mister_client(settings) as mister:
        run_capture_lineups(db, mister, ff_client(), run_date=DAY, now=NOW)
    path = tmp_path / "r.csv"
    idstore.export_review(db, path)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert rows and all(r["decision"] == "" for r in rows)
    # Importar sin tocar no cambia nada.
    with db.transaction():
        st = idstore.import_review(db, path)
    assert (st.linked, st.ignored, st.skipped) == (0, 0, len(rows))


def test_coverage_recent_gameweeks(db: Conn, settings: Settings) -> None:
    seed(db)
    with db.transaction():
        repo.insert_rows(
            db, "gameweeks",
            [_gw(4047, 6), _gw(4048, 7)], ["id"], update=True,
        )  # fmt: skip
        # Un jugador sin emparejar que solo jugó en la J6.
        db.execute("insert into players (mister_player_id, name, team_id) values (999, 'Nadie', 4)")
        db.execute(
            "insert into player_gameweek (player_id, gameweek_id, minutes) values (999, 4047, 10)"
        )
    with mister_client(settings) as mister:
        run_capture_lineups(db, mister, ff_client(), run_date=DAY, now=NOW)
    season = idstore.coverage(db, "futbolfantasy")
    recent = idstore.coverage(db, "futbolfantasy", recent_gameweeks=1)
    assert season.total == len(MISTER_PLAYERS) + 1 and season.matched == len(MISTER_PLAYERS)
    assert recent.total == len(MISTER_PLAYERS) and recent.rate == 1.0


def _gw(gid: int, number: int) -> Any:
    from mister_assistant.store.normalize import GameweekRow

    return GameweekRow(gid, number, "26/27", "regular", "finished", None, None)


def test_search_players(db: Conn) -> None:
    seed(db)
    hits = idstore.search_players(db, "cucho hernandez")
    assert hits[0][0] == 103 and hits[0][2] == "Betis"
    assert idstore.search_players(db, "raul moro", team="osasuna")[0][0] == 204
    assert idstore.search_players(db, "raul moro", team="betis")[0][0] != 204


def test_import_accepts_excel_csv_and_never_overwrites(
    db: Conn, settings: Settings, tmp_path: Path
) -> None:
    seed(db)
    with mister_client(settings) as mister:
        run_capture_lineups(db, mister, ff_client(), run_date=DAY, now=NOW)
    pending = db.execute(
        "select external_id from identity_review where status = 'pending' order by external_id"
    ).fetchall()
    first, second = pending[0][0], pending[1][0]
    # Como lo guarda Excel en español: BOM, «;» y CRLF.
    text = (
        "source;external_id;external_name;team;candidates;sugerencia;decision\r\n"
        f"futbolfantasy;{first};Uno;Betis;;; 777 \r\n"
        f"futbolfantasy;{second};Dos;Betis;;;101\r\n"  # 101 ya está emparejado (Marc Roca)
    )
    path = tmp_path / "excel.csv"
    path.write_bytes(("﻿" + text).encode("utf-8"))
    with db.transaction():
        st = idstore.import_review(db, path)
    assert st.linked == 1 and len(st.conflicts) == 1
    assert db.execute(
        "select player_id, verified from player_xref where external_id = %s", (first,)
    ).fetchone() == (777, True)
    assert db.execute("select external_id from player_xref where player_id = 101").fetchone() == (
        "3215",
    )


def test_import_rejects_file_without_columns(db: Conn, tmp_path: Path) -> None:
    import pytest

    path = tmp_path / "mal.csv"
    path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(idstore.ReviewFileError):
        idstore.import_review(db, path)
