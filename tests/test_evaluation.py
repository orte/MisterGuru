"""Fase 5: backtest, modelo v1 y evaluación semanal."""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import psycopg
import pytest
from psycopg.types.json import Jsonb

from mister_assistant.evals import backtest as bt
from mister_assistant.evals.weekly import audit_market, build_weekly, evaluate_gameweek
from mister_assistant.models import points_v1 as v1

Conn = psycopg.Connection[tuple[Any, ...]]


def test_spearman_with_ties() -> None:
    a = np.asarray([1.0, 2.0, 2.0, 4.0])
    assert bt.spearman(a, a) == pytest.approx(1.0)
    assert bt.spearman(a, -a) == pytest.approx(-1.0)
    assert math.isnan(bt.spearman(a, np.zeros(4)))


def test_v1_only_replaces_v0_when_clearly_better() -> None:
    better = bt.Comparison([4, 5], 2.0, 1.8, -0.2, -0.3, -0.1, lineup_v0=100, lineup_v1=105)
    assert better.v1_better
    noisy = bt.Comparison([4, 5], 2.0, 1.98, -0.02, -0.05, 0.01, 100, 105)
    assert not noisy.v1_better
    worse_lineups = bt.Comparison([4, 5], 2.0, 1.8, -0.2, -0.3, -0.1, 100, 90)
    assert not worse_lineups.v1_better


def test_calibration_bins() -> None:
    preds = {i: bt.Prediction(i, 3, 1.0, p) for i, p in enumerate([0.1, 0.15, 0.9, 0.95])}
    real = {2: (5, True, True), 3: (0, False, False)}
    cal = bt.calibration(preds, real)
    assert [(lo, n) for lo, _, _, n in cal] == [(0.0, 2), (0.8, 2)]
    assert cal[1][2] == pytest.approx(0.5)


def test_v1_learns_that_good_history_means_more_points() -> None:
    rng = random.Random(0)
    rows = []
    for i in range(600):
        quality = rng.random()  # media reciente de picas como señal
        rating = min(4, max(0, round(quality * 4 + rng.gauss(0, 0.6))))
        x = [3.0, 4.0, 0.8, quality * 14, quality * 14, quality * 14, 6 + quality * 2,
             quality * 10, math.nan, 0.0, 0.0, 1.0, math.nan]  # fmt: skip
        rows.append(
            v1.Row(i, 1, x, (v1.rating_class(rating),) * 3, 6.0 + quality * 2 + rng.gauss(0, 0.3))
        )
    model = v1.fit_v1(rows)
    low = model.expected_base([3.0, 4.0, 0.8, 1, 1, 1, 6.1, 1, math.nan, 0, 0, 1, math.nan])
    high = model.expected_base([3.0, 4.0, 0.8, 13, 13, 13, 7.9, 9, math.nan, 0, 0, 1, math.nan])
    assert high > low + 4


def test_rating_classes_follow_points_order() -> None:
    idx = [v1.rating_class(r) for r in (0, None, 1, 2, 3, 4)]
    assert idx == [0, 1, 2, 3, 4, 5]
    assert [v1.CLASS_POINTS[i] for i in idx] == sorted(v1.CLASS_POINTS)


# -- integración ------------------------------------------------------------------

KICKOFF = datetime(2026, 10, 9, 19, 0, tzinfo=UTC)


def seed_closed_gameweek(db: Conn) -> None:
    """J8 cerrada: predicción guardada antes, once puesto y puntos reales."""
    squad = [(100, 1)] + [(200 + i, 2) for i in range(5)] + [(300 + i, 3) for i in range(5)]
    squad += [(400 + i, 4) for i in range(3)]
    xi = [100, 200, 201, 202, 203, 300, 301, 302, 303, 400, 401]
    with db.transaction(), db.cursor() as cur:
        cur.execute(
            "insert into gameweeks (id, number, status, first_match_at)"
            " values (4049, 8, 'finished', %s)",
            (KICKOFF,),
        )
        cur.execute(
            "insert into fixtures (id, gameweek_id, home_team_id, away_team_id, kickoff_at, status)"
            " values (1, 4049, 4, 50, %s, 'played')",
            (KICKOFF,),
        )
        cur.executemany(
            "insert into players (mister_player_id, name, position, team_id)"
            " values (%s, %s, %s, 4)",
            [(p, f"J{p}", pos) for p, pos in squad],
        )
        # Reales: el 304 y el 402 (en el banquillo) hacen 10; el resto 3.
        cur.executemany(
            "insert into player_gameweek (player_id, gameweek_id, match_id, minutes, points_final)"
            " values (%s, 4049, 1, 90, %s)",
            [(p, 10 if p in (304, 402) else 3) for p, _ in squad],
        )
        cur.execute(
            "insert into prediction_runs (id, gameweek_id, model_version, created_at,"
            " first_kickoff_at, before_kickoff, trigger) values"
            " (1, 4049, 'v0.1', %s, %s, true, 'vispera')",
            (KICKOFF - timedelta(days=1), KICKOFF),
        )
        cur.executemany(
            "insert into predictions (run_id, player_id, gameweek_id, position, p_start, p_sub,"
            " p_play, exp_minutes, exp_points, p20, p80) values"
            " (1, %s, 4049, %s, 0.9, 0, 0.9, 80, %s, 0, 6)",
            [(p, pos, 8.0 if p in (304, 402) else 3.0) for p, pos in squad],
        )
        rec = [100, 200, 201, 202, 203, 300, 301, 302, 304, 400, 402]
        cur.execute(
            "insert into lineup_recommendations (run_id, manager_id, gameweek_id, formation,"
            " player_ids, expected_points) values (1, 1, 4049, '4-4-2', %s, 43)",
            (rec,),
        )
        positions = {"1": {"1": {"id": 100}}}
        positions["2"] = {str(i): {"id": p} for i, p in enumerate(xi[1:5], 2)}
        positions["3"] = {str(i): {"id": p} for i, p in enumerate(xi[5:9], 6)}
        positions["4"] = {str(i): {"id": p} for i, p in enumerate(xi[9:], 10)}
        bench = [{"id": p} for p, _ in squad if p not in xi]
        cur.execute(
            "insert into raw_responses (source, route, params, params_key, run_date, status_code,"
            " body_json) values ('mister', 'gameweek', '{\"id\": \"4049\"}', 'g', %s, 200, %s)",
            (KICKOFF.date(), Jsonb({"data": {"lineup": {"positions": positions}, "bench": bench}})),
        )


def test_evaluate_closed_gameweek(db: Conn) -> None:
    seed_closed_gameweek(db)
    ge = evaluate_gameweek(db)
    assert ge is not None and ge.number == 8 and ge.run_id == 1
    assert ge.lineup is not None
    assert ge.lineup.recommended == 9 * 3 + 2 * 10  # el once enviado
    assert ge.lineup.played == 11 * 3
    assert ge.metrics.n == 14 and ge.metrics.mae == pytest.approx(
        2 * 2 / 14
    )  # dos jugadores con 2 de error


def test_market_audit_compares_with_the_rest_of_the_market(db: Conn) -> None:
    today = date(2026, 10, 20)
    day = today - timedelta(days=8)
    with db.transaction(), db.cursor() as cur:
        cur.executemany(
            "insert into player_value_daily (player_id, value_date, value, source)"
            " values (%s, %s, %s, 'snapshot')",
            [(1, day, 100), (1, today, 120), (2, day, 100), (2, today, 90)],
        )
        cur.executemany(
            "insert into market_snapshot (player_id, snapshot_date, on_sale, value_trend)"
            " values (%s, %s, true, 0)",
            [(1, day), (2, day)],
        )
        cur.execute(
            "insert into recommendations (report_date, kind, player_id, numbers, message)"
            " values (%s, 'fichaje', 1, '{}', 'x')",
            (day,),
        )
    with db.transaction():
        ma = audit_market(db, today)
    assert ma.audited == 1
    assert ma.buy_value_change == pytest.approx(0.2)
    assert ma.avoided_value_change == pytest.approx(-0.1)
    outcome = db.execute("select outcome from recommendations").fetchone()
    assert outcome is not None and outcome[0]["dias"] == 8


def test_weekly_report_without_data_does_not_fail(db: Conn) -> None:
    report = build_weekly(db, with_backtest=False)
    assert "aún no hay ninguna cerrada" in report.message
