"""Puntos esperados v0 (PLAN §6, Fase 3), sin aprendizaje automático.

Por jugador y escenario (titular / suplente que entra / no juega):
- Titular: nivel propio (media de su Mixta sin bonus en sus titularidades,
  como diferencia con la media de su posición para el mismo resultado,
  regresada a 0) + media de su posición según el resultado esperado de su
  equipo (cuotas 1X2; sin cuotas, el reparto medio de la liga) + bonus de gol
  esperado (xG/90 regresado a su posición, por minutos y por goles esperados del
  partido) + tarjetas.
- Suplente: media de la liga para su posición (incluye los S.C. de las
  suplencias cortas, que cuentan 0).
- No juega: 0.
Media analítica; suelo (p20) y techo (p80) por simulación.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from mister_assistant.models.features import RESULTS, LeaguePriors, PlayerFeatures
from mister_assistant.models.minutes import MinutesForecast, forecast_minutes
from mister_assistant.scoring.mixed import GOAL_POINTS, round_mean

MODEL_VERSION = "v0.1"
SKILL_SHRINK = 4.0  # titularidades equivalentes de la media de la posición
XG_PRIOR_MINUTES = 450.0
SIMULATIONS = 4000


@dataclass(frozen=True)
class PointsForecast:
    player_id: int
    minutes: MinutesForecast
    exp_points: float
    p20: float
    p80: float
    components: dict[str, float] = field(default_factory=dict)


def result_probs(f: PlayerFeatures, priors: LeaguePriors) -> tuple[dict[str, float], bool]:
    probs = f.fixture.result_probs(f.team_id) if f.fixture else None
    if probs is None:
        return dict(priors.result_dist), False
    total = sum(probs.values())
    return {k: v / total for k, v in probs.items()}, True


def skill(f: PlayerFeatures, priors: LeaguePriors) -> float:
    """Nivel propio respecto a su posición, regresado a la media."""
    table = priors.base_by_result[f.position]
    diffs = [
        a.base_mix - table[a.result] for a in f.history if a.started and a.base_mix is not None
    ]
    return sum(diffs) / (len(diffs) + SKILL_SHRINK)


def team_goal_factor(f: PlayerFeatures, probs: dict[str, float], priors: LeaguePriors) -> float:
    if f.fixture is None or f.fixture.total_goals is None:
        return 1.0
    # Parte de los goles del partido que marca su equipo, según el favoritismo.
    share = min(max(0.5 + 0.5 * (probs["V"] - probs["D"]), 0.2), 0.8)
    return (f.fixture.total_goals * share) / priors.team_goals


def xg90(f: PlayerFeatures, priors: LeaguePriors) -> float:
    prior = priors.xg90[f.position]
    return (f.xg + prior * XG_PRIOR_MINUTES / 90) / (f.xg_minutes + XG_PRIOR_MINUTES) * 90


def forecast_points(
    f: PlayerFeatures,
    priors: LeaguePriors,
    *,
    simulations: int = SIMULATIONS,
    base_start: float | None = None,
) -> PointsForecast:
    """Puntos esperados. `base_start` sustituye la Mixta sin bonus del titular
    (la pone el modelo v1); minutos, bonus de gol y simulación son los mismos."""
    mins = forecast_minutes(f, priors)
    probs, from_odds = result_probs(f, priors)
    table = priors.base_by_result[f.position]
    level = skill(f, priors)
    if base_start is None:
        base_start = level + sum(probs[r] * table[r] for r in RESULTS)
    goal_factor = team_goal_factor(f, probs, priors)
    lam_start = xg90(f, priors) * mins.exp_minutes_start / 90 * goal_factor
    goal_pts = GOAL_POINTS[f.position]
    exp_start = base_start + goal_pts * lam_start + priors.card_ev
    exp_sub = priors.sub_mean[f.position]
    exp_points = mins.p_start * exp_start + mins.p_sub * exp_sub

    p20, p80 = _simulate(
        f.player_id, mins, base_start, priors.start_sd[f.position], lam_start, goal_pts,
        exp_sub, priors.sub_sd[f.position], simulations,
    )  # fmt: skip
    return PointsForecast(
        player_id=f.player_id,
        minutes=mins,
        exp_points=round(exp_points, 2),
        p20=p20,
        p80=p80,
        components={
            "nivel": round(level, 2),
            "base_titular": round(base_start, 2),
            "p_victoria": round(probs["V"], 3),
            "cuotas": float(from_odds),
            "goles_esperados": round(lam_start, 3),
            "esperado_titular": round(exp_start, 2),
            "esperado_suplente": round(exp_sub, 2),
        },
    )


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth: suficiente para λ pequeños (goles de un jugador en un partido).
    limit, k, p = pow(2.718281828459045, -lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _simulate(
    seed: int, mins: MinutesForecast, base_start: float, sd_start: float, lam: float,
    goal_pts: int, mean_sub: float, sd_sub: float, n: int,
) -> tuple[float, float]:  # fmt: skip
    if mins.p_play <= 0:
        return 0.0, 0.0
    rng = random.Random(seed)
    draws = []
    for _ in range(n):
        u = rng.random()
        if u < mins.p_start:
            x = rng.gauss(base_start, sd_start) + goal_pts * _poisson(rng, lam)
        elif u < mins.p_start + mins.p_sub:
            x = rng.gauss(mean_sub, sd_sub)
        else:
            x = 0.0
        draws.append(round_mean(x))
    draws.sort()
    return float(draws[int(0.2 * (n - 1))]), float(draws[int(0.8 * (n - 1))])
