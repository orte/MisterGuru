"""Minutos v0: P(titular), P(entrar desde el banquillo) y minutos esperados.

Parte de la probabilidad externa (Fútbol Fantasy o el once probable de Mister) y
la corrige con lo que ha hecho el jugador en los últimos partidos de su equipo.
"""

from __future__ import annotations

from dataclasses import dataclass

from mister_assistant.models.features import LeaguePriors, PlayerFeatures

# Peso de la fuente externa frente al historial propio.
FF_WEIGHT = 0.75
MISTER_WEIGHT = 0.6
MISTER_IN_XI = 0.85
MISTER_OUT_XI = 0.10
# P(entrar | no es titular) a priori, y su peso en partidos equivalentes.
SUB_PRIOR = {1: 0.02, 2: 0.40, 3: 0.50, 4: 0.50}
SUB_PRIOR_WEIGHT = 2.0
MINUTES_PRIOR_WEIGHT = 3.0


@dataclass(frozen=True)
class MinutesForecast:
    p_start: float
    p_sub: float  # incondicional: P(no es titular y entra)
    exp_minutes_start: float
    exp_minutes: float
    source: str  # futbolfantasy | mister | historial | sin_partido | baja

    @property
    def p_play(self) -> float:
        return self.p_start + self.p_sub


def history_start_rate(f: PlayerFeatures) -> float:
    n = len(f.history)
    starts = sum(a.started for a in f.history)
    return (starts + 0.5) / (n + 1)  # suavizado hacia 0,5 con un partido ficticio


def forecast_minutes(f: PlayerFeatures, priors: LeaguePriors) -> MinutesForecast:
    if f.fixture is None:
        return MinutesForecast(0.0, 0.0, 0.0, 0.0, "sin_partido")
    if f.ff_prob is not None and f.ff_prob == 0 and f.ff_injury not in (None, -1):
        return MinutesForecast(0.0, 0.0, 0.0, 0.0, "baja")

    hist = history_start_rate(f)
    if f.ff_prob is not None:
        p_start, source = FF_WEIGHT * f.ff_prob + (1 - FF_WEIGHT) * hist, "futbolfantasy"
    elif f.mister_xi is not None:
        ext = MISTER_IN_XI if f.mister_xi else MISTER_OUT_XI
        p_start, source = MISTER_WEIGHT * ext + (1 - MISTER_WEIGHT) * hist, "mister"
    else:
        p_start, source = hist, "historial"
    p_start = min(max(p_start, 0.0), 0.99)

    non_starts = [a for a in f.history if not a.started]
    came_on = sum(a.played for a in non_starts)
    prior = SUB_PRIOR[f.position]
    p_sub_given = (came_on + prior * SUB_PRIOR_WEIGHT) / (len(non_starts) + SUB_PRIOR_WEIGHT)
    p_sub = (1 - p_start) * p_sub_given

    start_mins = [a.minutes for a in f.history if a.started]
    prior_min = priors.start_minutes[f.position]
    exp_min_start = (sum(start_mins) + prior_min * MINUTES_PRIOR_WEIGHT) / (
        len(start_mins) + MINUTES_PRIOR_WEIGHT
    )
    exp_minutes = p_start * exp_min_start + p_sub * priors.sub_minutes
    return MinutesForecast(p_start, p_sub, exp_min_start, exp_minutes, source)
