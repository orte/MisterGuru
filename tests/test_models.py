from __future__ import annotations

import pytest

from mister_assistant.models.features import (
    DEFAULT_PRIORS,
    Appearance,
    FixtureContext,
    PlayerFeatures,
    poisson_total_from_over,
)
from mister_assistant.models.minutes import forecast_minutes
from mister_assistant.models.points import forecast_points

FIX = FixtureContext(1, home_team_id=4, away_team_id=50, p_home=0.6, p_draw=0.25, p_away=0.15,
                     total_goals=2.6)  # fmt: skip


def starter_history(base: float, n: int = 5, result: str = "V") -> list[Appearance]:
    return [Appearance(7 - i, True, True, 90, int(base), base, result) for i in range(n)]


def player(**kw: object) -> PlayerFeatures:
    defaults: dict[str, object] = dict(
        player_id=1, name="X", position=3, team_id=4, fixture=FIX, history=[], xg=0.0,
        xg_minutes=0,
    )  # fmt: skip
    defaults.update(kw)
    return PlayerFeatures(**defaults)  # type: ignore[arg-type]


def test_poisson_inversion() -> None:
    lam = poisson_total_from_over(0.5)
    assert 2.6 < lam < 2.75  # P(≥3) = 0,5 ⇔ λ ≈ 2,67


def test_no_fixture_means_no_play() -> None:
    mf = forecast_minutes(player(fixture=None), DEFAULT_PRIORS)
    assert mf.p_play == 0 and mf.source == "sin_partido"


def test_injured_with_zero_probability() -> None:
    mf = forecast_minutes(player(ff_prob=0.0, ff_injury=2), DEFAULT_PRIORS)
    assert mf.p_play == 0 and mf.source == "baja"


def test_external_probability_dominates_history() -> None:
    hist_bench = [Appearance(7 - i, False, False, 0, None, None, "V") for i in range(5)]
    mf = forecast_minutes(player(history=hist_bench, ff_prob=0.9), DEFAULT_PRIORS)
    assert 0.65 < mf.p_start < 0.8 and mf.source == "futbolfantasy"
    mf = forecast_minutes(player(history=starter_history(5)), DEFAULT_PRIORS)
    assert mf.p_start > 0.85 and mf.source == "historial"


def test_better_player_and_favourite_team_score_more() -> None:
    avg = forecast_points(player(history=starter_history(3.5)), DEFAULT_PRIORS)
    star = forecast_points(player(history=starter_history(8.0)), DEFAULT_PRIORS)
    assert star.exp_points > avg.exp_points
    underdog = FixtureContext(1, 4, 50, p_home=0.15, p_draw=0.25, p_away=0.6, total_goals=2.6)
    weak = forecast_points(player(history=starter_history(3.5), fixture=underdog), DEFAULT_PRIORS)
    assert avg.exp_points > weak.exp_points


def test_goal_threat_adds_points_for_forwards() -> None:
    hist = starter_history(4.0)
    plain = forecast_points(player(position=4, history=hist), DEFAULT_PRIORS)
    scorer = forecast_points(player(position=4, history=hist, xg=4.0, xg_minutes=450),
                             DEFAULT_PRIORS)  # fmt: skip
    assert scorer.exp_points > plain.exp_points + 0.5
    assert scorer.components["goles_esperados"] > plain.components["goles_esperados"]


def test_floor_and_ceiling_bracket_the_mean() -> None:
    pf = forecast_points(player(history=starter_history(5.0)), DEFAULT_PRIORS)
    assert pf.p20 <= pf.exp_points <= pf.p80
    again = forecast_points(player(history=starter_history(5.0)), DEFAULT_PRIORS)
    assert (pf.p20, pf.p80) == (again.p20, again.p80)  # determinista


def test_non_player_scores_zero() -> None:
    pf = forecast_points(player(fixture=None), DEFAULT_PRIORS)
    assert pf.exp_points == 0 and pf.p20 == pf.p80 == 0


@pytest.mark.parametrize("prob", [0.0, 0.5, 1.0])
def test_probabilities_are_valid(prob: float) -> None:
    mf = forecast_minutes(player(ff_prob=prob, ff_injury=-1), DEFAULT_PRIORS)
    assert 0 <= mf.p_start <= 1 and 0 <= mf.p_sub <= 1 and mf.p_play <= 1
