"""Calibración del motor Mixto contra las puntuaciones reales de Mister.

Recalcula cada fila de `player_gameweek` con todas las combinaciones de las dos
incógnitas (redondeo y bonus con S.C.), elige la que más acierta y explica las
discrepancias que quedan.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import product
from typing import get_args

from mister_assistant.scoring.mixed import (
    SOURCES,
    MixedPoints,
    Rounding,
    mixed_points,
)
from mister_assistant.store.normalize import PlayerGameweekRow


@dataclass(frozen=True)
class Hypothesis:
    rounding: Rounding
    bonus_on_sc: bool

    def label(self) -> str:
        return f"redondeo={self.rounding}, bonus_con_SC={'sí' if self.bonus_on_sc else 'no'}"


@dataclass(frozen=True)
class Discrepancy:
    player_id: int
    gameweek_id: int
    expected: int
    computed: int
    cause: str
    detail: str


@dataclass
class CalibrationReport:
    total_rows: int
    usable_rows: int
    scores: dict[Hypothesis, int]
    best: Hypothesis | None
    matches: int
    target: str
    source_mismatches: Counter[str] = field(default_factory=Counter)
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        return self.matches / self.usable_rows if self.usable_rows else 0.0


def _compute(row: PlayerGameweekRow, h: Hypothesis) -> MixedPoints:
    assert row.position is not None
    return mixed_points(
        rating_as=row.rating_as,
        rating_marca=row.rating_marca,
        rating_md=row.rating_md,
        rating_sofascore=row.rating_sofascore,
        position=row.position,
        goals=row.goals,
        penalty_goals=row.penalty_goals,
        double_yellow=row.double_yellow,
        red_cards=row.red_cards,
        rounding=h.rounding,
        bonus_on_sc=h.bonus_on_sc,
    )


def _usable(row: PlayerGameweekRow) -> bool:
    return row.position in (1, 2, 3, 4) and row.points_mix is not None


def _mister_source_points(row: PlayerGameweekRow) -> dict[str, int | None]:
    return {
        "as": row.points_as,
        "marca": row.points_marca,
        "md": row.points_md,
        "sofascore": row.points_sofascore,
    }


def _explain(row: PlayerGameweekRow, mp: MixedPoints) -> tuple[str, str]:
    mister = _mister_source_points(row)
    diffs = [
        f"{s}: Mister {mister[s]} vs motor {mp.per_source[s]}"
        for s in SOURCES
        if mister[s] is not None and mister[s] != mp.per_source[s]
    ]
    if row.has_manual_rating:
        return "corrección manual de Mister", "; ".join(diffs)
    if diffs:
        sc = [s for s, r in _ratings(row).items() if r is None]
        cause = "fuente S.C." if sc else "puntos de una fuente distintos"
        return cause, "; ".join(diffs)
    mean = sum(v for v in mister.values() if v is not None) / 4
    return "redondeo de la media", f"media {mean:.2f}"


def _ratings(row: PlayerGameweekRow) -> dict[str, float | int | None]:
    return {
        "as": row.rating_as,
        "marca": row.rating_marca,
        "md": row.rating_md,
        "sofascore": row.rating_sofascore,
    }


def calibrate(rows: list[PlayerGameweekRow], target: str = "points_mix") -> CalibrationReport:
    """`target` = columna con la que comparar: `points_mix` (Mixta) o `points_final`."""
    usable = [r for r in rows if _usable(r) and getattr(r, target) is not None]
    hypotheses = [
        Hypothesis(rounding, bonus)
        for rounding, bonus in product(get_args(Rounding), (True, False))
    ]
    scores = {
        h: sum(1 for r in usable if _compute(r, h).total == getattr(r, target)) for h in hypotheses
    }
    best = max(hypotheses, key=lambda h: scores[h]) if usable else None
    report = CalibrationReport(
        total_rows=len(rows),
        usable_rows=len(usable),
        scores=scores,
        best=best,
        matches=scores[best] if best else 0,
        target=target,
    )
    if best is None:
        return report
    for r in usable:
        mp = _compute(r, best)
        for s, value in _mister_source_points(r).items():
            if value is not None and value != mp.per_source[s]:
                report.source_mismatches[s] += 1
        expected = int(getattr(r, target))
        if mp.total != expected:
            cause, detail = _explain(r, mp)
            report.discrepancies.append(
                Discrepancy(r.player_id, r.gameweek_id, expected, mp.total, cause, detail)
            )
    return report


def format_calibration(report: CalibrationReport, max_examples: int = 25) -> str:
    lines = [
        f"Filas: {report.total_rows} · utilizables: {report.usable_rows}"
        f" · objetivo: {report.target}",
    ]
    if report.best is None:
        lines.append("Sin filas utilizables: ejecuta antes backfill-gameweeks.")
        return "\n".join(lines)
    lines.append(
        f"Mejor hipótesis: {report.best.label()} → {report.matches}/{report.usable_rows}"
        f" ({report.match_rate:.2%})"
    )
    lines.append("Todas las hipótesis:")
    for h, n in sorted(report.scores.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {n:>5}  {h.label()}")
    if report.source_mismatches:
        lines.append(
            "Fuentes cuyo punto no cuadra: "
            + ", ".join(f"{s} {n}" for s, n in report.source_mismatches.most_common())
        )
    causes = Counter(d.cause for d in report.discrepancies)
    if causes:
        lines.append("Discrepancias por causa: " + ", ".join(f"{c} {n}" for c, n in causes.items()))
        for d in report.discrepancies[:max_examples]:
            lines.append(
                f"  jugador {d.player_id} jornada {d.gameweek_id}: Mister {d.expected},"
                f" motor {d.computed} — {d.cause} ({d.detail})"
            )
    return "\n".join(lines)
