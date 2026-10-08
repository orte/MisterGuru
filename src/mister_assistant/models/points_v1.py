"""Puntos v1: Mixta del titular aprendida por fuente con LightGBM (Fase 5).

Sustituye solo la «base del titular» de v0 (la Mixta sin bonus cuando es
titular); minutos, bonus de gol y simulación son los de v0.

- AS, Marca y MD: modelo **ordinal** (Frank y Hall) sobre los niveles ordenados
  por puntos: 0 estrellas (−2) < S.C. (0) < 1 (+2) < 2 (+6) < 3 (+10) < 4 (+14).
  Un clasificador binario por umbral, P(nivel ≥ k); de ahí la distribución y
  los puntos esperados de cada diario.
- SofaScore: regresión de la nota; los residuos del entrenamiento convierten la
  nota esperada en puntos esperados con la tabla oficial (no lineal).

Entradas por jugador y jornada, calculadas solo con lo anterior a la jornada:
medias recientes de cada fuente cuando fue titular, número de titularidades,
tasa de titularidad, posición, log del valor de mercado al empezar la jornada,
forma de su equipo y del rival, si juega en casa y su xG por 90 minutos.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import lightgbm as lgb
import numpy as np
import psycopg

from mister_assistant.models.features import PlayerFeatures
from mister_assistant.scoring.mixed import cronista_points, sofascore_points

Conn = psycopg.Connection[tuple[Any, ...]]
MODEL_VERSION = "v1.0"
CLASS_POINTS = (-2, 0, 2, 6, 10, 14)  # 0★, S.C., 1★, 2★, 3★, 4★
FEATURES = (
    "position", "n_starts", "start_rate", "as_pts", "marca_pts", "md_pts", "ss_rating",
    "base_mix", "log_value", "team_form", "opp_form", "home", "xg90",
)  # fmt: skip
PARAMS: dict[str, Any] = {
    "num_leaves": 7,
    "min_data_in_leaf": 25,
    "learning_rate": 0.05,
    "lambda_l2": 1.0,
    "feature_fraction": 0.9,
    "verbose": -1,
    "seed": 0,
    "deterministic": True,
    "num_threads": 1,
}
ROUNDS = 150
RESIDUAL_SAMPLE = 300


def rating_class(rating: int | None) -> int:
    """Índice del nivel en CLASS_POINTS (None = S.C.)."""
    if rating is None:
        return 1
    return {0: 0, 1: 2, 2: 3, 3: 4, 4: 5}[int(rating)]


@dataclass(frozen=True)
class Row:
    player_id: int
    gameweek_id: int
    x: list[float]
    # Objetivo (solo filas de entrenamiento: titulares de esa jornada)
    classes: tuple[int, int, int] | None = None
    ss_rating: float | None = None


def _nan_mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else math.nan


def team_form(conn: Conn, before_number: int) -> dict[int, float]:
    """Resultado medio (V=1, E=0, D=−1) de cada equipo antes de la jornada."""
    acc: dict[int, list[float]] = defaultdict(list)
    for home, away, gh, ga in conn.execute(
        "select f.home_team_id, f.away_team_id, f.goals_home, f.goals_away from fixtures f"
        " join gameweeks g on g.id = f.gameweek_id"
        " where f.status = 'played' and g.number < %s and f.goals_home is not null",
        (before_number,),
    ):
        s = (gh > ga) - (gh < ga)
        acc[int(home)].append(float(s))
        acc[int(away)].append(float(-s))
    return {t: sum(v) / len(v) for t, v in acc.items()}


def values_at(conn: Conn, gameweek_id: int) -> dict[int, int]:
    row = conn.execute(
        "select min(f.kickoff_at)::date from fixtures f where f.gameweek_id = %s", (gameweek_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return {}
    day = row[0] - timedelta(days=1)
    return {
        int(r[0]): int(r[1])
        for r in conn.execute(
            "select distinct on (player_id) player_id, value from player_value_daily"
            " where value_date <= %s order by player_id, value_date desc",
            (day,),
        )
    }


def make_rows(
    conn: Conn, features: list[PlayerFeatures], gameweek_id: int, number: int, *, with_target: bool
) -> list[Row]:
    form = team_form(conn, number)
    values = values_at(conn, gameweek_id)
    targets: dict[int, tuple[tuple[int, int, int], float | None]] = {}
    if with_target:
        for pid, r_as, r_marca, r_md, r_ss in conn.execute(
            "select player_id, rating_as, rating_marca, rating_md, rating_sofascore"
            " from player_gameweek where gameweek_id = %s and sub_in_minute is null"
            " and minutes > 0",
            (gameweek_id,),
        ):
            targets[int(pid)] = (
                (rating_class(r_as), rating_class(r_marca), rating_class(r_md)),
                float(r_ss) if r_ss is not None else None,
            )
    rows: list[Row] = []
    for f in features:
        if f.fixture is None:
            continue
        starts = [a for a in f.history if a.started]
        home = f.fixture.home_team_id == f.team_id
        opp = f.fixture.away_team_id if home else f.fixture.home_team_id
        value = values.get(f.player_id)
        x = [
            float(f.position),
            float(len(starts)),
            (len(starts) + 0.5) / (len(f.history) + 1),
            _nan_mean([cronista_points(a.ratings[0]) for a in starts]),
            _nan_mean([cronista_points(a.ratings[1]) for a in starts]),
            _nan_mean([cronista_points(a.ratings[2]) for a in starts]),
            _nan_mean([a.ratings[3] for a in starts if a.ratings[3] is not None]),
            _nan_mean([a.base_mix for a in starts if a.base_mix is not None]),
            math.log(value) if value else math.nan,
            form.get(f.team_id, math.nan),
            form.get(opp, math.nan),
            1.0 if home else 0.0,
            f.xg / f.xg_minutes * 90 if f.xg_minutes >= 90 else math.nan,
        ]
        t = targets.get(f.player_id)
        if with_target and t is None:
            continue
        rows.append(Row(f.player_id, gameweek_id, x, t[0] if t else None, t[1] if t else None))
    return rows


@dataclass
class V1Model:
    ordinal: list[list[lgb.Booster | float]]  # por diario, por umbral k=1..5
    sofascore: lgb.Booster | None
    ss_mean: float
    residuals: np.ndarray
    n_train: int

    def expected_base(self, x: list[float]) -> float:
        arr = np.asarray([x], dtype=float)
        per_source = []
        for models in self.ordinal:
            ge = [float(m.predict(arr)[0]) if isinstance(m, lgb.Booster) else m for m in models]
            ge = list(np.minimum.accumulate(np.clip(ge, 0, 1)))  # P(≥k) no creciente
            cum = [1.0, *ge, 0.0]
            probs = [max(cum[k] - cum[k + 1], 0.0) for k in range(len(CLASS_POINTS))]
            total = sum(probs) or 1.0
            per_source.append(
                sum(p * pts for p, pts in zip(probs, CLASS_POINTS, strict=True)) / total
            )
        rating = float(self.sofascore.predict(arr)[0]) if self.sofascore else self.ss_mean
        per_source.append(float(np.mean([sofascore_points(rating + r) for r in self.residuals])))
        return float(np.mean(per_source))


def fit_v1(rows: list[Row]) -> V1Model:
    x = np.asarray([r.x for r in rows], dtype=float).reshape(-1, len(FEATURES))
    ordinal: list[list[lgb.Booster | float]] = []
    for source in range(3):
        y_idx = np.asarray([r.classes[source] for r in rows if r.classes], dtype=int)
        models: list[lgb.Booster | float] = []
        for k in range(1, len(CLASS_POINTS)):
            y = (y_idx >= k).astype(float)
            if y.min() == y.max() or min(y.sum(), len(y) - y.sum()) < 10:
                models.append(float(y.mean()))  # sin variación suficiente: constante
                continue
            ds = lgb.Dataset(x, y, feature_name=list(FEATURES), free_raw_data=False)
            models.append(lgb.train({**PARAMS, "objective": "binary"}, ds, ROUNDS))
        ordinal.append(models)
    ss_rows = [(r.x, r.ss_rating) for r in rows if r.ss_rating is not None]
    ss_x = np.asarray([a for a, _ in ss_rows], dtype=float).reshape(-1, len(FEATURES))
    ss_y = np.asarray([b for _, b in ss_rows], dtype=float)
    booster = None
    residuals = np.zeros(1)
    if len(ss_y) >= 50:
        ds = lgb.Dataset(ss_x, ss_y, feature_name=list(FEATURES), free_raw_data=False)
        booster = lgb.train({**PARAMS, "objective": "regression"}, ds, ROUNDS)
        res = ss_y - booster.predict(ss_x)
        rng = np.random.default_rng(0)
        residuals = rng.choice(res, size=min(RESIDUAL_SAMPLE, len(res)), replace=False)
    return V1Model(ordinal, booster, float(ss_y.mean()) if len(ss_y) else 6.7, residuals, len(rows))
