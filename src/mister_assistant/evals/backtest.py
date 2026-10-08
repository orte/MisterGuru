"""Backtest «como si» por jornada cerrada (Fase 5).

Para cada jornada objetivo se predice con lo que se sabía antes de ella (historial,
xG y parámetros de la liga solo de jornadas anteriores; sin Fútbol Fantasy ni
cuotas, que no se capturaban entonces) y se compara con lo que pasó:

- Puntos esperados: error absoluto medio, sesgo y correlación de rangos
  (Spearman) sobre todos los jugadores de los equipos que jugaron.
- P(jugar): calibración por tramos (predicho frente a observado).
- Once: el recomendado con la plantilla que tenías, el que pusiste y el de
  «los 11 más caros», todos con puntos reales.

v1 se entrena en cada jornada solo con las anteriores (validación temporal).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import numpy as np
import psycopg

from mister_assistant.decide.lineup import Option, best_lineup
from mister_assistant.models import points_v1 as v1
from mister_assistant.models.features import PlayerFeatures, load_player_features, load_priors
from mister_assistant.models.points import forecast_points

Conn = psycopg.Connection[tuple[Any, ...]]
CALIBRATION_BINS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0001)
SIMULATIONS = 300


@dataclass(frozen=True)
class Prediction:
    player_id: int
    position: int
    exp_points: float
    p_play: float


@dataclass
class Metrics:
    n: int
    mae: float
    bias: float  # media de (predicho − real)
    spearman: float
    starters_mae: float  # solo quienes fueron titulares


@dataclass
class LineupComparison:
    recommended: int
    played: int
    most_expensive: int
    recommended_formation: str


@dataclass
class GameweekResult:
    number: int
    gameweek_id: int
    metrics: dict[str, Metrics]
    lineups: dict[str, LineupComparison]
    calibration: list[tuple[float, float, float, int]]  # tramo, P predicha, observada, n
    # Error absoluto por jugador y modelo (para la comparación pareada)
    abs_errors: dict[str, dict[int, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class Comparison:
    gameweeks: list[int]
    mae_v0: float
    mae_v1: float
    diff: float  # MAE v1 − MAE v0 (negativo = v1 mejor)
    ci_low: float
    ci_high: float
    lineup_v0: int
    lineup_v1: int

    @property
    def v1_better(self) -> bool:
        """v1 solo sustituye a v0 si mejora con claridad: todo el intervalo por
        debajo de 0 y sin elegir peores onces."""
        return self.ci_high < 0 and self.lineup_v1 >= self.lineup_v0


@dataclass
class BacktestReport:
    gameweeks: list[GameweekResult] = field(default_factory=list)

    def compare(self, resamples: int = 2000) -> Comparison | None:
        """v0 frente a v1 en las mismas jornadas, con bootstrap pareado del MAE."""
        common = [g for g in self.gameweeks if "v0" in g.abs_errors and "v1" in g.abs_errors]
        if not common:
            return None
        d = np.asarray(
            [
                g.abs_errors["v1"][p] - g.abs_errors["v0"][p]
                for g in common
                for p in g.abs_errors["v0"]
            ]
        )
        e0 = np.asarray([e for g in common for e in g.abs_errors["v0"].values()])
        rng = np.random.default_rng(0)
        boots = [float(rng.choice(d, size=len(d)).mean()) for _ in range(resamples)]
        lo, hi = np.percentile(boots, [2.5, 97.5])
        return Comparison(
            [g.number for g in common], float(e0.mean()), float(e0.mean() + d.mean()),
            float(d.mean()), float(lo), float(hi),
            sum(g.lineups["v0"].recommended for g in common if "v0" in g.lineups),
            sum(g.lineups["v1"].recommended for g in common if "v1" in g.lineups),
        )  # fmt: skip

    def summary(self, model: str) -> Metrics | None:
        ms = [g.metrics[model] for g in self.gameweeks if model in g.metrics]
        if not ms:
            return None
        n = sum(m.n for m in ms)
        return Metrics(
            n,
            sum(m.mae * m.n for m in ms) / n,
            sum(m.bias * m.n for m in ms) / n,
            float(np.mean([m.spearman for m in ms])),
            float(np.mean([m.starters_mae for m in ms])),
        )


def actuals(conn: Conn, gameweek_id: int) -> dict[int, tuple[int, bool, bool]]:
    """Puntos reales, si jugó y si fue titular."""
    return {
        int(r[0]): (int(r[1] or 0), bool(r[2]), r[3] is None and bool(r[2]))
        for r in conn.execute(
            "select player_id, points_final, minutes > 0, sub_in_minute from player_gameweek"
            " where gameweek_id = %s",
            (gameweek_id,),
        )
    }


def predict_v0(conn: Conn, features: list[PlayerFeatures], number: int) -> dict[int, Prediction]:
    priors = load_priors(conn, before_number=number)
    out = {}
    for f in features:
        pf = forecast_points(f, priors, simulations=SIMULATIONS)
        out[f.player_id] = Prediction(f.player_id, f.position, pf.exp_points, pf.minutes.p_play)
    return out


def predict_v1(
    conn: Conn,
    features: list[PlayerFeatures],
    gameweek_id: int,
    number: int,
    train_rows: list[v1.Row],
) -> dict[int, Prediction]:
    priors = load_priors(conn, before_number=number)
    model = v1.fit_v1(train_rows)
    rows = {
        r.player_id: r for r in v1.make_rows(conn, features, gameweek_id, number, with_target=False)
    }
    out = {}
    for f in features:
        r = rows.get(f.player_id)
        base = model.expected_base(r.x) if r else None
        pf = forecast_points(f, priors, simulations=SIMULATIONS, base_start=base)
        out[f.player_id] = Prediction(f.player_id, f.position, pf.exp_points, pf.minutes.p_play)
    return out


def _ranks(a: np.ndarray) -> np.ndarray:
    """Rangos con empates promediados."""
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    ranks[order] = np.arange(len(a), dtype=float)
    _, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=ranks)
    out: np.ndarray = sums[inv] / counts[inv]
    return out


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = _ranks(a), _ranks(b)
    if ra.std() == 0 or rb.std() == 0:
        return math.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def metrics(preds: dict[int, Prediction], real: dict[int, tuple[int, bool, bool]]) -> Metrics:
    pids = list(preds)
    p = np.asarray([preds[i].exp_points for i in pids])
    y = np.asarray([real.get(i, (0, False, False))[0] for i in pids], dtype=float)
    starters = [k for k, i in enumerate(pids) if real.get(i, (0, False, False))[2]]
    rho = spearman(p, y) if len(pids) > 2 else math.nan
    return Metrics(
        n=len(pids),
        mae=float(np.mean(np.abs(p - y))),
        bias=float(np.mean(p - y)),
        spearman=float(rho),
        starters_mae=float(np.mean(np.abs(p[starters] - y[starters]))) if starters else math.nan,
    )


def abs_errors(
    preds: dict[int, Prediction], real: dict[int, tuple[int, bool, bool]]
) -> dict[int, float]:
    return {i: abs(pr.exp_points - real.get(i, (0, False, False))[0]) for i, pr in preds.items()}


def calibration(
    preds: dict[int, Prediction], real: dict[int, tuple[int, bool, bool]]
) -> list[tuple[float, float, float, int]]:
    out = []
    for lo, hi in pairwise(CALIBRATION_BINS):
        group = [i for i, pr in preds.items() if lo <= pr.p_play < hi]
        if not group:
            continue
        mean_p = float(np.mean([preds[i].p_play for i in group]))
        observed = float(np.mean([real.get(i, (0, False, False))[1] for i in group]))
        out.append((lo, mean_p, observed, len(group)))
    return out


def my_lineup(conn: Conn, gameweek_id: int) -> tuple[list[int], list[int]]:
    """(once puesto, plantilla) de la jornada, del crudo de /ajax/sw/gameweek."""
    row = conn.execute(
        "select body_json->'data' from raw_responses where source = 'mister'"
        " and route = 'gameweek' and params->>'id' = %s order by captured_at desc limit 1",
        (str(gameweek_id),),
    ).fetchone()
    if row is None or row[0] is None:
        return [], []
    data = row[0]
    xi = [
        int(p["id"])
        for slots in ((data.get("lineup") or {}).get("positions") or {}).values()
        for p in (slots.values() if isinstance(slots, dict) else slots)
        if isinstance(p, dict) and p.get("id")
    ]
    bench = [int(p["id"]) for p in data.get("bench") or [] if p.get("id")]
    return xi, xi + [b for b in bench if b not in xi]


def compare_lineups(
    conn: Conn,
    gameweek_id: int,
    preds: dict[int, Prediction],
    real: dict[int, tuple[int, bool, bool]],
) -> LineupComparison | None:
    xi, squad = my_lineup(conn, gameweek_id)
    if len(xi) < 11:
        return None
    positions = {
        int(r[0]): int(r[1])
        for r in conn.execute(
            "select mister_player_id, position from players where mister_player_id = any(%s)",
            (squad,),
        )
    }
    values = v1.values_at(conn, gameweek_id)

    def opts(score: dict[int, float]) -> list[Option]:
        return [
            Option(p, str(p), positions[p], score.get(p, 0.0), 1.0) for p in squad if p in positions
        ]

    def real_points(ids: list[int]) -> int:
        return sum(real.get(i, (0, False, False))[0] for i in ids)

    rec = best_lineup(opts({p: pr.exp_points for p, pr in preds.items()}))
    rich = best_lineup(opts({p: float(values.get(p, 0)) for p in squad}))
    return LineupComparison(
        recommended=real_points(rec.player_ids),
        played=real_points(xi),
        most_expensive=real_points(rich.player_ids),
        recommended_formation=rec.formation,
    )


def run_backtest(
    conn: Conn, numbers: list[int] | None = None, min_v1_train: int = 3
) -> BacktestReport:
    gameweeks = [
        (int(r[0]), int(r[1]))
        for r in conn.execute(
            "select id, number from gameweeks where status in ('finished', 'ongoing')"
            " and id in (select distinct gameweek_id from player_gameweek) order by number"
        )
    ]
    feats_cache: dict[int, list[PlayerFeatures]] = {}
    rows_cache: dict[int, list[v1.Row]] = {}

    def feats(gid: int) -> list[PlayerFeatures]:
        if gid not in feats_cache:
            feats_cache[gid] = load_player_features(conn, gid, backtest=True)
        return feats_cache[gid]

    report = BacktestReport()
    for idx, (gid, number) in enumerate(gameweeks):
        if numbers is not None and number not in numbers:
            continue
        if idx < 2:
            continue  # sin historial suficiente
        real = actuals(conn, gid)
        f = feats(gid)
        preds = {"v0": predict_v0(conn, f, number)}
        train_gws = gameweeks[:idx]
        if len(train_gws) >= min_v1_train:
            train: list[v1.Row] = []
            for tgid, tnum in train_gws:
                if tgid not in rows_cache:
                    rows_cache[tgid] = v1.make_rows(conn, feats(tgid), tgid, tnum, with_target=True)
                train.extend(rows_cache[tgid])
            preds["v1"] = predict_v1(conn, f, gid, number, train)
        result = GameweekResult(
            number, gid,
            {m: metrics(p, real) for m, p in preds.items()},
            {m: lc for m, p in preds.items() if (lc := compare_lineups(conn, gid, p, real))},
            calibration(preds["v0"], real),
            {m: abs_errors(p, real) for m, p in preds.items()},
        )  # fmt: skip
        report.gameweeks.append(result)
    return report


def format_backtest(report: BacktestReport) -> str:
    lines = ["Backtest por jornada (predicho con lo anterior a cada jornada, sin FF ni cuotas)", ""]
    lines.append(
        "J   modelo  n    MAE   sesgo  Spearman  MAE titulares | once rec / puesto / caros"
    )
    for g in report.gameweeks:
        for m, mt in g.metrics.items():
            lc = g.lineups.get(m)
            xi = (
                f"{lc.recommended:>3} / {lc.played:>3} / {lc.most_expensive:>3}"
                f" ({lc.recommended_formation})"
                if lc
                else "—"
            )
            lines.append(
                f"J{g.number:<2} {m:<6} {mt.n:<4} {mt.mae:5.2f} {mt.bias:+6.2f}  {mt.spearman:7.3f}"
                f"  {mt.starters_mae:6.2f}        | {xi}"
            )
    lines.append("")
    for m in ("v0", "v1"):
        s = report.summary(m)
        if s:
            lines.append(
                f"Total {m}: MAE {s.mae:.3f} · sesgo {s.bias:+.3f} · Spearman {s.spearman:.3f}"
                f" · MAE titulares {s.starters_mae:.3f} (n={s.n})"
            )
    for m in ("v0", "v1"):
        lcs = [g.lineups[m] for g in report.gameweeks if m in g.lineups]
        if lcs:
            lines.append(
                f"Once {m}: recomendado {sum(c.recommended for c in lcs)}"
                f" · puesto {sum(c.played for c in lcs)}"
                f" · los 11 más caros {sum(c.most_expensive for c in lcs)}"
                f" ({len(lcs)} jornadas)"
            )
    cmp = report.compare()
    if cmp:
        verdict = "v1 sustituye a v0" if cmp.v1_better else "se mantiene v0"
        lines += [
            "",
            f"v0 frente a v1 en J{cmp.gameweeks[0]}–J{cmp.gameweeks[-1]}: MAE {cmp.mae_v0:.3f}"
            f" frente a {cmp.mae_v1:.3f} (diferencia {cmp.diff:+.3f},"
            f" IC 95 % [{cmp.ci_low:+.3f}, {cmp.ci_high:+.3f}]) · once recomendado"
            f" {cmp.lineup_v0} frente a {cmp.lineup_v1} puntos reales → {verdict}",
        ]
    cal = [c for g in report.gameweeks for c in g.calibration]
    if cal:
        lines += ["", "Calibración de P(jugar) (v0): tramo · predicha · observada · n"]
        for lo in CALIBRATION_BINS[:-1]:
            grp = [c for c in cal if c[0] == lo]
            if grp:
                n = sum(c[3] for c in grp)
                pred = sum(c[1] * c[3] for c in grp) / n
                obs = sum(c[2] * c[3] for c in grp) / n
                lines.append(f"  {lo:.1f}+  {pred:.2f}  {obs:.2f}  {n}")
    return "\n".join(lines)
