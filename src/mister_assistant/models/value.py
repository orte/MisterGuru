"""Variación esperada del valor de mercado a 7 y 14 días (Fase 4).

Regresión lineal con regularización (ridge) sobre el logaritmo de la variación:

    log(v[t+h] / v[t]) ~ tendencia a 1, 3, 7 y 14 días + log(valor) + puntos recientes

Se ajusta en cada ejecución con `player_value_daily` (≈ 150.000 filas) y se valida
dejando fuera las últimas fechas. Si en esa validación no mejora a suponer que
el valor no cambia, se usa esa referencia (variación 0): mejor no predecir
nada que predecir ruido.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import psycopg

Conn = psycopg.Connection[tuple[Any, ...]]
HORIZONS = (7, 14)
LAGS = (1, 3, 7, 14)
RIDGE = 1.0
HOLDOUT_DAYS = 28
MIN_TRAIN = 300
MIN_TEST = 100
FEATURES = ("r1", "r3", "r7", "r14", "log_value", "points_vs_position")


@dataclass(frozen=True)
class Series:
    player_id: int
    position: int | None
    values: dict[date, int]


@dataclass(frozen=True)
class FitReport:
    horizon: int
    n_train: int
    n_test: int
    mae_model: float  # error absoluto medio de log(v[t+h]/v[t]) en validación
    mae_zero: float  # referencia: el valor no cambia
    mae_momentum: float  # referencia: sigue la tendencia de la última semana
    use_model: bool
    coefficients: dict[str, float]


@dataclass(frozen=True)
class ValueForecast:
    player_id: int
    value: int
    change_7d: float  # fracción, p. ej. 0,05 = +5 %
    change_14d: float

    @property
    def expected_value_14d(self) -> int:
        return round(self.value * (1 + self.change_14d))


def load_series(conn: Conn) -> list[Series]:
    by_player: dict[int, dict[date, int]] = defaultdict(dict)
    for pid, day, value in conn.execute(
        "select player_id, value_date, value from player_value_daily where value > 0"
    ):
        by_player[int(pid)][day] = int(value)
    positions = {
        int(r[0]): r[1] for r in conn.execute("select mister_player_id, position from players")
    }
    return [Series(pid, positions.get(pid), vals) for pid, vals in by_player.items()]


def load_points_signal(conn: Conn) -> dict[int, list[tuple[date, float]]]:
    """(fecha de cierre de la jornada, Mixta − media de su posición) por jugador."""
    rows = conn.execute(
        "select pg.player_id, g.last_match_at::date, pg.points_mix,"
        " avg(pg.points_mix) over (partition by pg.position)"
        " from player_gameweek pg join gameweeks g on g.id = pg.gameweek_id"
        " where pg.points_mix is not null and g.last_match_at is not null"
    ).fetchall()
    out: dict[int, list[tuple[date, float]]] = defaultdict(list)
    for pid, day, pts, avg in rows:
        out[int(pid)].append((day, float(pts) - float(avg)))
    for v in out.values():
        v.sort()
    return out


def _features(s: Series, day: date, points: list[tuple[date, float]] | None) -> list[float] | None:
    v = s.values.get(day)
    if not v:
        return None
    feats: list[float] = []
    for lag in LAGS:
        past = s.values.get(day - timedelta(days=lag))
        if not past:
            return None
        feats.append(math.log(v / past))
    feats.append(math.log(v))
    recent = [p for d, p in (points or []) if day - timedelta(days=21) < d <= day]
    feats.append(sum(recent) / len(recent) if recent else 0.0)
    return feats


def build_dataset(
    series: list[Series], points: dict[int, list[tuple[date, float]]], horizon: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    xs, ys, mom, days = [], [], [], []
    for s in series:
        for day in s.values:
            future = s.values.get(day + timedelta(days=horizon))
            if not future:
                continue
            f = _features(s, day, points.get(s.player_id))
            if f is None:
                continue
            xs.append(f)
            ys.append(math.log(future / s.values[day]))
            mom.append(f[2] * horizon / 7)  # tendencia de 7 días, extrapolada
            days.append(day.toordinal())
    return (
        np.asarray(xs, dtype=float).reshape(-1, len(FEATURES)),
        np.asarray(ys, dtype=float),
        np.asarray(mom, dtype=float),
        np.asarray(days, dtype=int),
    )


def _ridge(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu, sd = x.mean(axis=0), x.std(axis=0)
    sd[sd == 0] = 1.0
    z = np.hstack([np.ones((len(x), 1)), (x - mu) / sd])
    penalty = RIDGE * np.eye(z.shape[1])
    penalty[0, 0] = 0.0
    beta = np.linalg.solve(z.T @ z + penalty, z.T @ y)
    return beta, mu, sd


def _predict(beta: np.ndarray, mu: np.ndarray, sd: np.ndarray, x: np.ndarray) -> np.ndarray:
    z = np.hstack([np.ones((len(x), 1)), (x - mu) / sd])
    out: np.ndarray = np.clip(z @ beta, -0.5, 0.5)
    return out


@dataclass
class ValueModel:
    reports: dict[int, FitReport]
    params: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]

    def predict(
        self, s: Series, day: date, points: list[tuple[date, float]] | None
    ) -> ValueForecast | None:
        value = s.values.get(day)
        f = _features(s, day, points)
        if value is None:
            return None
        changes: dict[int, float] = {}
        for h in HORIZONS:
            if f is None or not self.reports[h].use_model:
                changes[h] = 0.0
                continue
            beta, mu, sd = self.params[h]
            changes[h] = float(np.expm1(_predict(beta, mu, sd, np.asarray([f]))[0]))
        return ValueForecast(s.player_id, value, changes[7], changes[14])


def fit(series: list[Series], points: dict[int, list[tuple[date, float]]]) -> ValueModel:
    reports: dict[int, FitReport] = {}
    params: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for h in HORIZONS:
        x, y, mom, days = build_dataset(series, points, h)
        if len(y) < 500:
            reports[h] = FitReport(h, len(y), 0, math.nan, math.nan, math.nan, False, {})
            continue
        # Validación temporal: el objetivo de las filas de entrenamiento no puede
        # solaparse con el periodo de validación.
        cutoff = days.max() - HOLDOUT_DAYS
        train = days <= cutoff - h
        test = days > cutoff
        if train.sum() < MIN_TRAIN or test.sum() < MIN_TEST:
            reports[h] = FitReport(
                h, int(train.sum()), int(test.sum()), math.nan, math.nan, math.nan, False, {}
            )
            continue
        beta, mu, sd = _ridge(x[train], y[train])
        pred = _predict(beta, mu, sd, x[test])
        mae_model = float(np.mean(np.abs(pred - y[test])))
        mae_zero = float(np.mean(np.abs(y[test])))
        mae_mom = float(np.mean(np.abs(mom[test] - y[test])))
        use = mae_model < mae_zero
        # Para producción, se reajusta con todo.
        params[h] = _ridge(x, y)
        reports[h] = FitReport(
            h, int(train.sum()), int(test.sum()), mae_model, mae_zero, mae_mom, use,
            dict(zip(("intercepto", *FEATURES), (float(b) for b in params[h][0]), strict=True)),
        )  # fmt: skip
    return ValueModel(reports, params)


def forecast_all(conn: Conn, on: date | None = None) -> tuple[ValueModel, dict[int, ValueForecast]]:
    series = load_series(conn)
    points = load_points_signal(conn)
    model = fit(series, points)
    out: dict[int, ValueForecast] = {}
    for s in series:
        day = on or max(s.values)
        fc = model.predict(s, day, points.get(s.player_id))
        if fc is not None:
            out[s.player_id] = fc
    return model, out
