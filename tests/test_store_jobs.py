"""Integración: migraciones + jobs contra Postgres real y Mister simulado."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import psycopg
from conftest import load_fixture

from mister_assistant.config import Settings
from mister_assistant.jobs.backfill_gameweeks import run_backfill
from mister_assistant.jobs.calibrate import run_calibration
from mister_assistant.jobs.snapshot_daily import run_snapshot_daily
from mister_assistant.sources.mister import MisterClient
from mister_assistant.store.db import migrate, pending_migrations

FIX = Path(__file__).parent / "fixtures" / "mister"
DAY = date(2026, 10, 8)
Conn = psycopg.Connection[tuple[Any, ...]]

PLAYED = {
    # id: (equipo, partido, posición, eventos) — datos reales de la jornada 7
    4836153: (2, 38071, 4, [
        {"category": "goal", "minute": 59}, {"category": "sub_out", "minute": 62},
    ]),
    71021: (2, 38071, 2, [{"category": "penalty", "minute": 53}]),
    63788: (15, 38071, 2, [{"category": "red", "minute": 52}]),
    53111: (4, 38073, 1, False),
    59789: (16, 38078, 4, [
        {"category": "sub_in", "minute": 61}, {"category": "yellow", "minute": 96},
        {"category": "double", "minute": 96},
    ]),
}  # fmt: skip


def finished_gameweek() -> dict[str, Any]:
    """Jornada 7 cerrada, reducida a los cinco jugadores con desglose en fixtures."""
    gw: dict[str, Any] = copy.deepcopy(load_fixture("mister/gameweek_finished.json"))
    players: dict[str, Any] = {}
    for pid, (team, match, pos, events) in PLAYED.items():
        entry = {"id": pid, "name": f"Jugador {pid}", "position": pos, "id_team": team,
                 "id_match": match, "points": 0, "played": 1, "status": "played",
                 "short": None, "events": events, "livePoints": None}  # fmt: skip
        players.setdefault(str(match), {"all": {}})["all"].setdefault(str(team), []).append(entry)
    gw["data"]["players"] = players
    # Los partidos de esos jugadores, jugados y puntuados.
    template = gw["data"]["games"][0]
    gw["data"]["games"] = [
        {**template, "id": int(match), "id_home": int(next(iter(teams))), "id_away": 0}
        for match, teams in ((m, v["all"]) for m, v in players.items())
    ]
    return gw


def current_gameweek() -> dict[str, Any]:
    gw = finished_gameweek()
    gw["data"]["players"] = []
    return gw


class FakeMister:
    """Mister simulado con fixtures; cuenta peticiones por ruta."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.user = load_fixture("mister/user.json")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append(path)
        if path == "/ajax/sw/gameweek":
            body = finished_gameweek() if "id" in form else current_gameweek()
            return httpx.Response(200, json=body)
        if path == "/ajax/player-gameweek":
            return httpx.Response(
                200, json=load_fixture(f"mister/player_gameweek_{form['id_player']}.json")
            )
        if path == "/ajax/sw/users":
            if form["id"] == "10974336":  # mánager sin plantilla: Mister da 404
                return httpx.Response(404)
            body = copy.deepcopy(self.user)
            body["data"]["id"] = int(form["id"])
            return httpx.Response(200, json=body)
        if path == "/ajax/balance":
            return httpx.Response(200, json=load_fixture("balance.json"))
        if path == "/ajax/feed":
            body = load_fixture("mister/feed_page.json")
            body["cfg"]["auth"] = "x-auth-que-no-debe-guardarse"
            if form.get("offset") != "0":
                body["data"] = []
            return httpx.Response(200, json=body)
        if path in ("/standings", "/market"):
            return httpx.Response(200, text=(FIX / f"{path[1:]}.html").read_text(encoding="utf-8"))
        return httpx.Response(404)


def client_for(settings: Settings, fake: FakeMister) -> MisterClient:
    return MisterClient(settings, transport=httpx.MockTransport(fake), sleep=lambda _: None)


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"select count(*) from {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_migrations_are_idempotent(db: Conn) -> None:
    assert pending_migrations(db) == []
    assert migrate(db) == []
    assert count(db, "player_gameweek") == 0


def test_snapshot_daily_end_to_end(db: Conn, settings: Settings) -> None:
    fake = FakeMister()
    with client_for(settings, fake) as client:
        result = run_snapshot_daily(db, client, run_date=DAY)
    assert result.status == "ok", result.errors
    assert count(db, "managers") == 14
    assert count(db, "manager_snapshot") == 14
    assert result.stats["mánagers sin plantilla"] == 1
    assert "/ajax/sw/users" in fake.calls and fake.calls.count("/ajax/sw/users") == 13
    empty = db.execute(
        "select season_rank, season_points, squad_size from manager_snapshot"
        " where manager_id = 10974336"
    ).fetchone()
    assert empty == (14, 55, 0)
    assert count(db, "market_snapshot") == 4
    assert count(db, "league_events") > 0
    assert count(db, "fixtures") == 3
    me = db.execute(
        "select m.is_me, s.balance, s.max_bid from managers m"
        " join manager_snapshot s on s.manager_id = m.mister_manager_id where m.is_me"
    ).fetchall()
    assert me == [(True, 4_299_770, 20_699_770)]

    # Nada de credenciales en lo crudo.
    raw = db.execute("select body_json::text from raw_responses where route = 'feed'").fetchone()
    assert raw is not None and "x-auth-que-no-debe-guardarse" not in raw[0]

    # Segunda ejecución del mismo día: se salta.
    with client_for(settings, fake) as client:
        again = run_snapshot_daily(db, client, run_date=DAY)
    assert again.status == "skipped" and again.requests == 0

    # Forzada: repite peticiones pero no duplica filas.
    before = {t: count(db, t) for t in ("manager_snapshot", "squad_snapshot", "league_events")}
    with client_for(settings, fake) as client:
        forced = run_snapshot_daily(db, client, run_date=DAY, force=True)
    assert forced.status == "ok"
    assert {t: count(db, t) for t in before} == before


def test_snapshot_isolates_failing_source(db: Conn, settings: Settings) -> None:
    fake = FakeMister()

    def broken_market(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/market":
            return httpx.Response(200, text='<div id="partial-content">sin lista</div>')
        return fake(request)

    with MisterClient(
        settings, transport=httpx.MockTransport(broken_market), sleep=lambda _: None
    ) as client:
        result = run_snapshot_daily(db, client, run_date=DAY)
    assert result.status == "partial"
    assert any("mercado" in e for e in result.errors)
    assert count(db, "manager_snapshot") == 14  # el resto se guardó


def test_session_expired_fails_job(db: Conn, settings: Settings) -> None:
    with MisterClient(
        settings, transport=httpx.MockTransport(lambda r: httpx.Response(401)), sleep=lambda _: None
    ) as client:
        result = run_snapshot_daily(db, client, run_date=DAY)
    assert result.session_expired and result.status == "failed"
    row = db.execute("select status from job_runs").fetchone()
    assert row == ("failed",)


def test_backfill_is_resumable_and_calibrates(db: Conn, settings: Settings) -> None:
    fake = FakeMister()
    with client_for(settings, fake) as client:
        first = run_backfill(db, client, numbers=[7], max_requests=4, run_date=DAY)
    assert first.status == "partial"
    partial_rows = count(db, "player_gameweek")
    assert 0 < partial_rows < 5

    with client_for(settings, fake) as client:
        second = run_backfill(db, client, numbers=[7], run_date=DAY)
    assert second.status == "ok", second.errors
    assert count(db, "player_gameweek") == 5
    # Solo pidió los que faltaban.
    assert fake.calls.count("/ajax/player-gameweek") == 5

    row = db.execute(
        "select points_mix, goals, penalty_goals, red_cards, double_yellow from player_gameweek"
        " where player_id = 71021"
    ).fetchone()
    assert row == (7, 0, 1, 0, 0)

    report = run_calibration(db)
    assert report.usable_rows == 5 and report.matches == 5


def test_backfill_aborts_cleanly_when_db_connection_is_lost(
    pg_admin_uri: str, db: Conn, settings: Settings
) -> None:
    from mister_assistant.store.db import connect

    conninfo = db.info.dsn
    job_conn = connect(conninfo)
    fake = FakeMister()

    def dropping(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ajax/player-gameweek" and fake.calls.count(request.url.path) == 2:
            job_conn.close()  # corte con Supabase durante el tercer desglose
        return fake(request)

    with MisterClient(
        settings, transport=httpx.MockTransport(dropping), sleep=lambda _: None
    ) as client:
        result = run_backfill(job_conn, client, numbers=[7], run_date=DAY)
    assert result.status == "failed"
    assert any("conexión con la base de datos perdida" in e for e in result.errors)
    # No sigue pidiendo a Mister tras perder la BD.
    assert fake.calls.count("/ajax/player-gameweek") == 3


def test_raw_payload_is_valid_json(db: Conn, settings: Settings) -> None:
    with client_for(settings, FakeMister()) as client:
        run_backfill(db, client, numbers=[7], run_date=DAY)
    rows = db.execute("select route, body_json from raw_responses").fetchall()
    assert {r[0] for r in rows} >= {"gameweek", "player_gameweek"}
    assert all(json.dumps(r[1]) for r in rows)
