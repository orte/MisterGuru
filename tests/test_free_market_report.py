"""Informe diario del mercado libre: ¿merece la pena pujar por algún libre?"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg
from test_market_report import NOW, TODAY, seed

from mister_assistant.jobs import free_market_report as fmr

Conn = psycopg.Connection[tuple[Any, ...]]


def test_only_free_players_with_open_deadline(db: Conn) -> None:
    seed(db)
    with db.transaction():
        # 601 lo vende un rival; un tercero, libre, ya cerró.
        db.execute("update market_snapshot set seller_manager_id = 2 where player_id = 601")
        db.execute(
            "insert into players (mister_player_id, name, position, team_id)"
            " values (602, 'J602', 4, 4)"
        )
        db.execute(
            "insert into market_snapshot (player_id, snapshot_date, market_value, value_trend,"
            " on_sale, sale_price, sale_ends_at) values (602, %s, 5000000, 0, true, 5000000, %s)",
            (TODAY, NOW - timedelta(hours=1)),
        )
    report = fmr.build_free_market_report(db, now=NOW)
    assert report.free_count == 1  # solo el 600
    assert [p.player_id for p in report.worth_it] == [600]
    assert "sí, merece la pena pujar" in report.message
    assert "Para puntuar" in report.message and "J601" not in report.message


def test_nothing_worth_it(db: Conn) -> None:
    seed(db)
    with db.transaction():
        db.execute("update market_snapshot set seller_manager_id = 2")  # todos de rivales
    report = fmr.build_free_market_report(db, now=NOW)
    assert report.free_count == 0 and not report.worth_it
    assert "no merece la pena pujar hoy" in report.message


def test_saved_once_per_day(db: Conn) -> None:
    seed(db)
    report = fmr.build_free_market_report(db, now=NOW)
    assert not fmr.already_sent(db, report.report_date)
    with db.transaction():
        n = fmr.save(db, report)
    assert n == len(report.worth_it) > 0
    assert fmr.already_sent(db, report.report_date)
    row = db.execute("select kind, numbers->>'motivo' from recommendations limit 1").fetchone()
    assert row == ("puja", "puntos")


def test_player_arriving_after_kickoff_counts_from_next_gameweek(db: Conn) -> None:
    from test_market_report import KICKOFF

    seed(db)
    with db.transaction():
        db.execute(
            "update market_snapshot set sale_ends_at = %s where player_id = 600",
            (KICKOFF + timedelta(hours=6),),
        )
    report = fmr.build_free_market_report(db, now=NOW)
    (p,) = [p for p in report.worth_it if p.player_id == 600]
    assert p.misses_next and p.counted_gameweeks == 4
    assert "llega tras el primer partido" in report.message
    # Un nombre con punto («A. Oroz») no se toca al formatear los decimales.
    assert ", " not in fmr._pts(2.5) and fmr._pts(2.5) == "2,5"
