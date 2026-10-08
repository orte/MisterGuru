"""Integración Fase 3: predicciones, once recomendado e informe contra Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import psycopg

from mister_assistant.config import Settings
from mister_assistant.decide.lineup import FORMATIONS
from mister_assistant.jobs import gameweek_report as gr
from mister_assistant.sources.mister import MisterClient
from mister_assistant.store import repo

Conn = psycopg.Connection[tuple[Any, ...]]
KICKOFF = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)
ME = 15448471

# Plantilla propia: 2 POR, 5 DEF, 5 CEN, 3 DEL repartidos entre Betis (4) y Osasuna (50).
SQUAD = (
    [(100 + i, f"Portero {i}", 1, 4 if i == 0 else 50) for i in range(2)]
    + [(200 + i, f"Defensa {i}", 2, 4 if i % 2 else 50) for i in range(5)]
    + [(300 + i, f"Medio {i}", 3, 4 if i % 2 else 50) for i in range(5)]
    + [(400 + i, f"Delantero {i}", 4, 4 if i % 2 else 50) for i in range(3)]
)
CURRENT_XI = [100, 200, 201, 202, 203, 300, 301, 302, 303, 400, 401]


def seed(db: Conn) -> None:
    with db.transaction():
        repo.upsert_teams(db, {4: "Betis", 50: "Osasuna"})
        db.execute(
            "insert into gameweeks (id, number, season, type, status, first_match_at)"
            " values (4048, 7, '26/27', 'regular', 'finished', %s),"
            "        (4049, 8, '26/27', 'regular', 'unstarted', %s)",
            (KICKOFF - timedelta(days=21), KICKOFF),
        )
        db.execute(
            "insert into fixtures (id, gameweek_id, home_team_id, away_team_id, kickoff_at,"
            " status, goals_home, goals_away) values"
            " (38001, 4049, 4, 50, %s, 'fixture', null, null),"
            " (38073, 4048, 4, 50, %s, 'played', 2, 0)",
            (KICKOFF, KICKOFF - timedelta(days=21)),
        )
        db.execute(
            "insert into managers (mister_manager_id, community_id, name, slug, is_me)"
            " values (%s, 1, 'Yo', 'yo', true)",
            (ME,),
        )
        with db.cursor() as cur:
            cur.executemany(
                "insert into players (mister_player_id, name, position, team_id)"
                " values (%s, %s, %s, %s)",
                SQUAD,
            )
            # La jornada pasada todos fueron titulares; el 404 no jugó.
            cur.executemany(
                "insert into player_gameweek (player_id, gameweek_id, match_id, team_id, position,"
                " minutes, points_mix, points_final) values (%s, 4048, 38073, %s, %s, 90, %s, %s)",
                [(p, t, pos, 3 + (p % 7), 3 + (p % 7)) for p, _, pos, t in SQUAD if p != 402],
            )
        db.execute(
            "insert into odds (event_id, captured_at, fixture_id, home_team_id, away_team_id,"
            " market, outcome, point, price_avg, prob_fair, bookmakers) values"
            " ('e', now(), 38001, 4, 50, 'h2h', 'home', 0, 1.6, 0.6, 10),"
            " ('e', now(), 38001, 4, 50, 'h2h', 'draw', 0, 4.0, 0.25, 10),"
            " ('e', now(), 38001, 4, 50, 'h2h', 'away', 0, 6.0, 0.15, 10),"
            " ('e', now(), 38001, 4, 50, 'totals', 'over', 2.5, 1.9, 0.5, 10),"
            " ('e', now(), 38001, 4, 50, 'totals', 'under', 2.5, 1.9, 0.5, 10)"
        )


def fake_mister(settings: Settings) -> MisterClient:
    def handler(request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.url.path == "/ajax/sw/gameweek":
            assert form.get("id") == "4049"
            positions = {"1": {"1": {"id": CURRENT_XI[0]}}}
            positions["2"] = {str(i): {"id": pid} for i, pid in enumerate(CURRENT_XI[1:5], 2)}
            positions["3"] = {str(i): {"id": pid} for i, pid in enumerate(CURRENT_XI[5:9], 6)}
            positions["4"] = {str(i): {"id": pid} for i, pid in enumerate(CURRENT_XI[9:], 10)}
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {"id_manager": ME, "lineup": {"positions": positions}},
                },
            )
        if request.url.path == "/ajax/sw/users":
            team = [{"id": p} for p, *_ in SQUAD]
            return httpx.Response(200, json={"status": "ok", "data": {"id": ME, "team_now": team}})
        return httpx.Response(404)

    return MisterClient(settings, transport=httpx.MockTransport(handler), sleep=lambda _: None)


def test_slots_and_next_gameweek(db: Conn) -> None:
    seed(db)
    gw = gr.next_gameweek(db, KICKOFF - timedelta(hours=30))
    assert gw is not None and (gw.gameweek_id, gw.number) == (4049, 8)
    assert gr.due_slot(db, gw, KICKOFF - timedelta(hours=30)) is None
    assert gr.due_slot(db, gw, KICKOFF - timedelta(hours=24)) == "vispera"
    assert gr.due_slot(db, gw, KICKOFF - timedelta(hours=10)) is None
    assert gr.due_slot(db, gw, KICKOFF - timedelta(hours=3)) == "previa"
    with db.transaction():
        gr.log_sent(db, gw, "previa", None, True)
    assert gr.due_slot(db, gw, KICKOFF - timedelta(hours=3)) is None
    assert gr.next_gameweek(db, KICKOFF + timedelta(minutes=1)) is None


def test_build_report_saves_predictions_and_valid_lineup(db: Conn, settings: Settings) -> None:
    seed(db)
    now = KICKOFF - timedelta(hours=24)
    gw = gr.next_gameweek(db, now)
    assert gw is not None
    with fake_mister(settings) as mister:
        result = gr.build_report(db, mister, gw, slot="vispera", now=now)

    lineup = result.lineup
    assert lineup is not None and len(lineup.players) == 11 and lineup.empty_slots == 0
    assert lineup.formation in FORMATIONS
    squad_ids = {p for p, *_ in SQUAD}
    assert set(lineup.player_ids) <= squad_ids
    assert sum(1 for p in lineup.players if p.position == 1) == 1

    run = db.execute(
        "select gameweek_id, before_kickoff, trigger from prediction_runs where id = %s",
        (result.run_id,),
    ).fetchone()
    assert run == (4049, True, "vispera")
    n = db.execute("select count(*) from predictions where run_id = %s", (result.run_id,))
    assert n.fetchone() == (len(SQUAD),)
    rec = db.execute(
        "select player_ids, current_player_ids, expected_points >= current_expected"
        " from lineup_recommendations where run_id = %s",
        (result.run_id,),
    ).fetchone()
    assert rec is not None and sorted(rec[0]) == sorted(lineup.player_ids)
    assert rec[1] == CURRENT_XI and rec[2] is True

    msg = result.message or ""
    assert "J8 · once recomendado" in msg and "Tu once actual" in msg
    assert msg.count("\nPOR  ") == 1


def test_started_gameweek_is_not_next(db: Conn) -> None:
    seed(db)
    with db.transaction():
        # Segundo partido de la J8 el sábado; el primero (viernes) ya empezó.
        db.execute(
            "insert into fixtures (id, gameweek_id, home_team_id, away_team_id, kickoff_at, status)"
            " values (38002, 4049, 50, 4, %s, 'fixture')",
            (KICKOFF + timedelta(days=1),),
        )
    assert gr.next_gameweek(db, KICKOFF + timedelta(hours=1)) is None
