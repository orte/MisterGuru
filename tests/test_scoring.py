from __future__ import annotations

import pytest
from conftest import load_fixture

from mister_assistant.scoring.calibration import calibrate, format_calibration
from mister_assistant.scoring.mixed import (
    cronista_points,
    extras,
    mixed_points,
    round_mean,
    sofascore_points,
)
from mister_assistant.store.normalize import PlayerGameweekRow, player_gameweek_row

# Eventos reales de la jornada 7 (id 4048) para los jugadores con fixture.
EVENTS = {
    4836153: ({"category": "goal", "minute": 59}, {"category": "sub_out", "minute": 62}),
    71021: ({"category": "penalty", "minute": 53},),
    63788: ({"category": "red", "minute": 52},),
    59789: (
        {"category": "sub_in", "minute": 61},
        {"category": "yellow", "minute": 96},
        {"category": "double", "minute": 96},
    ),
    53111: (),
}


def real_rows() -> list[PlayerGameweekRow]:
    return [
        player_gameweek_row(load_fixture(f"mister/player_gameweek_{pid}.json")["data"], ev)
        for pid, ev in EVENTS.items()
    ]


@pytest.mark.parametrize(
    ("rating", "points"),
    [(4, 14), (3, 10), (2, 6), (1, 2), (0, -2), (None, 0)],
)
def test_cronista_table(rating: int | None, points: int) -> None:
    assert cronista_points(rating) == points


@pytest.mark.parametrize(
    ("rating", "points"),
    [
        (10.0, 12), (9.3, 12), (9.2, 11), (8.6, 11), (8.5, 10), (8.0, 10), (7.9, 9),
        (7.8, 9), (7.7, 8), (7.6, 8), (7.5, 7), (7.4, 7), (7.3, 6), (7.2, 6), (7.1, 5),
        (7.0, 5), (6.9, 4), (6.8, 4), (6.7, 3), (6.6, 3), (6.5, 2), (6.4, 2), (6.3, 1),
        (6.2, 1), (6.1, 0), (6.0, 0), (5.9, -1), (5.8, -1), (5.7, -2), (5.4, -2),
        (5.3, -3), (5.0, -3), (4.9, -4), (0.0, -4), (None, 0),
    ],
)  # fmt: skip
def test_sofascore_table(rating: float | None, points: int) -> None:
    assert sofascore_points(rating) == points


def test_extras_by_position() -> None:
    assert [extras(p, goals=1) for p in (1, 2, 3, 4)] == [6, 5, 4, 3]
    assert extras(2, penalty_goals=1) == 3
    assert extras(4, double_yellow=1) == -3
    assert extras(3, red_cards=1) == -6


def test_official_example_sc_counts_as_zero() -> None:
    # Ayuda de Mister: AS 1 pica, Marca S.C., MD S.C., SofaScore 6.5 → 1 punto.
    mp = mixed_points(
        rating_as=1, rating_marca=None, rating_md=None, rating_sofascore=6.5, position=3
    )
    assert mp.per_source == {"as": 2, "marca": 0, "md": 0, "sofascore": 2}
    assert mp.total == 1


@pytest.mark.parametrize(
    ("value", "expected"), [(7.25, 7), (-6.25, -6), (-2.5, -3), (2.5, 3), (8.0, 8), (0.75, 1)]
)
def test_round_half_away_from_zero(value: float, expected: int) -> None:
    assert round_mean(value, "half_away") == expected


def test_engine_reproduces_real_breakdowns() -> None:
    for row in real_rows():
        assert row.position is not None
        mp = mixed_points(
            rating_as=row.rating_as,
            rating_marca=row.rating_marca,
            rating_md=row.rating_md,
            rating_sofascore=row.rating_sofascore,
            position=row.position,
            goals=row.goals,
            penalty_goals=row.penalty_goals,
            double_yellow=row.double_yellow,
            red_cards=row.red_cards,
        )
        assert mp.per_source == {
            "as": row.points_as,
            "marca": row.points_marca,
            "md": row.points_md,
            "sofascore": row.points_sofascore,
        }, row.player_id
        assert mp.total == row.points_mix == row.points_final, row.player_id


def test_calibration_picks_half_away_on_real_rows() -> None:
    report = calibrate(real_rows())
    assert report.usable_rows == 5
    assert report.matches == 5
    # -2,5 → -3 descarta el redondeo bancario y el truncado.
    assert report.best is not None and report.best.rounding == "half_away"
    assert "100.00%" in format_calibration(report)
