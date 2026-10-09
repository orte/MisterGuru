"""Integración Fase 4: estimación de saldos e informe de mercado contra Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from mister_assistant.decide.balances import estimate_balances
from mister_assistant.jobs.market_report import build_market_report, save_recommendations

Conn = psycopg.Connection[tuple[Any, ...]]
# Relativas al reloj: el código busca la próxima jornada con now() de Postgres.
NOW = datetime.now(UTC).replace(microsecond=0)
TODAY = NOW.date()
ME, RIVAL = 1, 2
KICKOFF = NOW + timedelta(days=1)
# 50 M − 13 iniciales × 5 M + 30 pts × 100 k + 200 k (2º de la jornada)
# − 10 M de compras + 2 M de ventas.
OWN_BALANCE = 50_000_000 - 65_000_000 + 3_000_000 + 200_000 - 10_000_000 + 2_000_000


def seed(db: Conn) -> None:
    my_squad = (
        [(100, 1)] + [(200 + i, 2) for i in range(5)] + [(300 + i, 3) for i in range(5)]
        + [(400 + i, 4) for i in range(3)]
    )  # fmt: skip
    rival_squad = [(500, 4), (501, 2)]
    market = [(600, 4), (601, 3)]
    with db.transaction(), db.cursor() as cur:
        cur.execute("insert into teams (id, name) values (4, 'Betis'), (50, 'Osasuna')")
        cur.execute(
            "insert into gameweeks (id, number, status, first_match_at) values"
            " (4048, 7, 'finished', %s), (4049, 8, 'unstarted', %s)",
            (NOW - timedelta(days=20), KICKOFF),
        )
        cur.execute(
            "insert into fixtures (id, gameweek_id, home_team_id, away_team_id, kickoff_at, status,"
            " goals_home, goals_away) values (1, 4049, 4, 50, %s, 'fixture', null, null),"
            " (2, 4048, 4, 50, %s, 'played', 1, 0)",
            (KICKOFF, NOW - timedelta(days=20)),
        )
        cur.execute(
            "insert into managers (mister_manager_id, community_id, name, is_me) values"
            " (1, 9, 'Yo', true), (2, 9, 'Rival', false)"
        )
        all_players = my_squad + rival_squad + market
        cur.executemany(
            "insert into players (mister_player_id, name, position, team_id)"
            " values (%s, %s, %s, %s)",
            [(p, f"J{p}", pos, 4 if p % 2 else 50) for p, pos in all_players],
        )
        # Estrella del rival (500) y del mercado (600): titulares que puntúan mucho.
        cur.executemany(
            "insert into player_gameweek (player_id, gameweek_id, match_id, team_id, position,"
            " minutes, points_mix, points_final) values (%s, 4048, 2, %s, %s, 90, %s, %s)",
            [
                (p, 4 if p % 2 else 50, pos, 14 if p in (500, 600) else 3,
                 14 if p in (500, 600) else 3)
                for p, pos in all_players
            ],
        )  # fmt: skip
        # 60 días de valores: el 600 sube cada día, el resto plano.
        cur.executemany(
            "insert into player_value_daily (player_id, value_date, value, source)"
            " values (%s, %s, %s, 'mister_chart')",
            [
                (p, TODAY - timedelta(days=d), int(5_000_000 * (0.99**d if p == 600 else 1)))
                for p, _ in all_players
                for d in range(60)
            ],
        )
        cur.execute(
            "insert into manager_snapshot (manager_id, snapshot_date, balance, max_bid, team_value)"
            " values (1, %s, %s, 30000000, 70000000), (2, %s, null, null, 40000000)",
            (TODAY, OWN_BALANCE, TODAY),
        )
        cur.executemany(
            "insert into squad_snapshot (player_id, snapshot_date, manager_id, market_value,"
            " clause_value, clause_floor) values (%s, %s, %s, 5000000, %s, 5000000)",
            [(p, TODAY, ME, 7_500_000) for p, _ in my_squad]
            # La estrella del rival tiene la cláusula casi en su valor: compensa.
            + [(p, TODAY, RIVAL, 5_200_000 if p == 500 else 7_500_000) for p, _ in rival_squad],
        )
        cur.executemany(
            "insert into market_snapshot (player_id, snapshot_date, market_value, value_trend,"
            " on_sale, sale_price) values (%s, %s, 5000000, 1, true, 5000000)",
            [(p, TODAY) for p, _ in market],
        )
        for mid, pts in ((ME, 30), (RIVAL, 50)):
            cur.execute(
                "insert into raw_responses (source, route, params, params_key, run_date,"
                " status_code, body_json) values ('mister', 'user', %s, %s, %s, 200, %s)",
                (
                    Jsonb({"id": str(mid)}), f"k{mid}", TODAY,
                    Jsonb({"data": {"userGameWeeks": {"4048": {"points": pts}}}}),
                ),
            )  # fmt: skip
        cur.executemany(
            "insert into league_events (event_key, community_id, feed_card_id, category,"
            " event_type, occurred_at, player_id, from_manager_id, to_manager_id, price, payload)"
            " values (%s, 9, 0, 'transfer', 'normal', %s, %s, %s, %s, %s, '{}')",
            [
                ("t1", NOW - timedelta(days=3), 300, None, ME, 10_000_000),
                ("t2", NOW - timedelta(days=2), 301, ME, None, 2_000_000),
                ("t3", NOW - timedelta(hours=3), 501, None, RIVAL, 5_500_000),
            ],
        )


def test_balance_estimator_matches_own_balance(db: Conn) -> None:
    seed(db)
    est = {b.manager_id: b for b in estimate_balances(db)}
    assert est[ME].error == 0
    # Iniciales propios: los 12 que nunca se movieron + el 301 (lo primero es una
    # venta); el 300 lo compró. A 5 M cada uno el día antes del primer traspaso.
    assert (est[ME].initial_players, est[ME].initial_value) == (13, 65_000_000)
    # Rival: 50 M − 1 inicial (500) + 50 pts × 100 k + 0 € (1º) − 5,5 M (compra del 501).
    assert est[RIVAL].initial_players == 1
    assert est[RIVAL].estimated == 50_000_000 - 5_000_000 + 5_000_000 - 5_500_000
    assert est[ME].rank_bonus == 200_000 and est[RIVAL].rank_bonus == 0


def test_market_report_end_to_end(db: Conn) -> None:
    seed(db)
    report = build_market_report(db, now=NOW)
    msg = report.message
    for section in (
        "🛒 Fichajes",
        "🎯 Cláusulas",
        "🛡️ Tus jugadores",
        "💸 Ventas",
        "👀 Movimientos",
    ):
        assert section in msg, section
    kinds = {r.kind for r in report.recommendations}
    assert "fichaje" in kinds and "clausula_ofensiva" in kinds
    fichaje = next(r for r in report.recommendations if r.kind == "fichaje")
    assert fichaje.player_id in (600, 601) and fichaje.numbers["puntos_horizonte"] > 0
    with db.transaction():
        n = save_recommendations(db, report)
    assert n == len(report.recommendations)
    row = db.execute("select count(*) from recommendations where report_date = %s", (TODAY,))
    assert row.fetchone() == (n,)


def test_backfill_values_universe_skips_fetched_players(db: Conn) -> None:
    from mister_assistant.jobs.backfill_values import universe

    seed(db)
    before = set(universe(db))
    assert 600 in before
    with db.transaction():
        # Ficha ya pedida aunque su serie sea corta (fichaje reciente).
        db.execute(
            "insert into raw_responses (source, route, params, params_key, run_date, status_code)"
            " values ('mister', 'player', '{\"id\": \"600\"}', 'p600', %s, 200)",
            (TODAY,),
        )
    assert 600 not in set(universe(db))
