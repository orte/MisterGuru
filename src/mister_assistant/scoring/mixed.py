"""Puntuación Mixta de Mister: media de AS, Marca, Mundo Deportivo y SofaScore.

Función pura. Las dos incógnitas de §2.1 (redondeo de la media y si el bonus de
gol/tarjeta se suma a una fuente S.C.) son parámetros; sus valores por defecto
son los que fija la calibración contra los datos reales (ver calibration.py).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Rounding = Literal["half_away", "half_even", "floor", "trunc"]

# Picas AS / estrellas Marca y MD → puntos. None = S.C. → 0.
_CRONISTA = {4: 14, 3: 10, 2: 6, 1: 2, 0: -2}

# Nota SofaScore (en décimas) → puntos: (umbral mínimo, puntos), de mayor a menor.
_SOFASCORE = (
    (93, 12), (86, 11), (80, 10), (78, 9), (76, 8), (74, 7), (72, 6), (70, 5),
    (68, 4), (66, 3), (64, 2), (62, 1), (60, 0), (58, -1), (54, -2), (50, -3),
)  # fmt: skip

# Puntos por gol según posición (1 portero … 4 delantero).
GOAL_POINTS = {1: 6, 2: 5, 3: 4, 4: 3}
PENALTY_GOAL_POINTS = 3
DOUBLE_YELLOW_POINTS = -3
RED_CARD_POINTS = -6

SOURCES = ("as", "marca", "md", "sofascore")

# Valores fijados por la calibración (docs/scoring-calibration.md).
DEFAULT_ROUNDING: Rounding = "half_away"
DEFAULT_BONUS_ON_SC = True


def cronista_points(rating: int | None) -> int:
    if rating is None:
        return 0
    try:
        return _CRONISTA[rating]
    except KeyError:
        raise ValueError(f"valoración de cronista fuera de rango: {rating}") from None


def sofascore_points(rating: float | None) -> int:
    if rating is None:
        return 0
    tenths = round(rating * 10)
    for threshold, points in _SOFASCORE:
        if tenths >= threshold:
            return points
    return -4


def extras(
    position: int,
    goals: int = 0,
    penalty_goals: int = 0,
    double_yellow: int = 0,
    red_cards: int = 0,
) -> int:
    """Bonus por eventos, igual en las cuatro fuentes. Gol en propia: 0."""
    if position not in GOAL_POINTS:
        raise ValueError(f"posición desconocida: {position}")
    return (
        GOAL_POINTS[position] * goals
        + PENALTY_GOAL_POINTS * penalty_goals
        + DOUBLE_YELLOW_POINTS * double_yellow
        + RED_CARD_POINTS * red_cards
    )


_ROUNDERS: dict[str, Callable[[float], int]] = {
    "half_away": lambda x: int(math.copysign(math.floor(abs(x) + 0.5), x)),
    "half_even": lambda x: round(x),
    "floor": math.floor,
    "trunc": math.trunc,
}


def round_mean(value: float, rounding: Rounding = DEFAULT_ROUNDING) -> int:
    return _ROUNDERS[rounding](value)


@dataclass(frozen=True)
class MixedPoints:
    per_source: dict[str, int]
    mean: float
    total: int


def mixed_points(
    *,
    rating_as: int | None,
    rating_marca: int | None,
    rating_md: int | None,
    rating_sofascore: float | None,
    position: int,
    goals: int = 0,
    penalty_goals: int = 0,
    double_yellow: int = 0,
    red_cards: int = 0,
    rounding: Rounding = DEFAULT_ROUNDING,
    bonus_on_sc: bool = DEFAULT_BONUS_ON_SC,
) -> MixedPoints:
    """Puntos Mixtos de un jugador en un partido. S.C. cuenta como 0 en la media."""
    bonus = extras(position, goals, penalty_goals, double_yellow, red_cards)
    ratings: dict[str, int | float | None] = {
        "as": rating_as,
        "marca": rating_marca,
        "md": rating_md,
        "sofascore": rating_sofascore,
    }
    per_source: dict[str, int] = {}
    for source, rating in ratings.items():
        if source == "sofascore":
            base = sofascore_points(rating)
        else:
            base = cronista_points(None if rating is None else int(rating))
        add = bonus if (rating is not None or bonus_on_sc) else 0
        per_source[source] = base + add
    mean = sum(per_source.values()) / len(per_source)
    return MixedPoints(per_source=per_source, mean=mean, total=round_mean(mean, rounding))
