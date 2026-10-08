"""Evaluación semanal (Fase 5): lo que se predijo y recomendó frente a lo que pasó.

1. Jornada: la última cerrada con predicciones guardadas antes del primer
   partido. Error y sesgo de los puntos esperados, correlación de rangos,
   calibración de P(jugar) y el once recomendado frente al puesto y a «los 11
   más caros», todo con puntos reales.
2. Mercado: cada recomendación con al menos 7 días se audita con lo que pasó
   (variación de valor y puntos desde entonces). Las de fichaje se comparan con
   el resto de jugadores que estaban en el mercado ese día (lo «evitado»).
3. Backtest «como si» de v0 frente a v1 con todas las jornadas, que decide si
   v1 sustituye a v0.

Todo queda en `evaluations` y se resume por Telegram.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from mister_assistant.evals import backtest as bt

Conn = psycopg.Connection[tuple[Any, ...]]
AUDIT_AFTER_DAYS = 7


@dataclass
class GameweekEvaluation:
    number: int
    gameweek_id: int
    run_id: int
    metrics: bt.Metrics
    lineup: bt.LineupComparison | None
    recommended_formation: str | None


def last_evaluable_gameweek(conn: Conn) -> tuple[int, int, int] | None:
    """(gameweek_id, número, run_id) de la última jornada cerrada con predicción previa."""
    row = conn.execute(
        "select g.id, g.number, ("
        "  select r.id from prediction_runs r where r.gameweek_id = g.id and r.before_kickoff"
        "  order by r.created_at desc limit 1)"
        " from gameweeks g where g.status = 'finished'"
        " and exists (select 1 from prediction_runs r"
        "  where r.gameweek_id = g.id and r.before_kickoff)"
        " and exists (select 1 from player_gameweek p where p.gameweek_id = g.id)"
        " order by g.number desc limit 1"
    ).fetchone()
    return (int(row[0]), int(row[1]), int(row[2])) if row and row[2] else None


def evaluate_gameweek(conn: Conn) -> GameweekEvaluation | None:
    found = last_evaluable_gameweek(conn)
    if found is None:
        return None
    gid, number, run_id = found
    preds = {
        int(r[0]): bt.Prediction(int(r[0]), int(r[1] or 0), float(r[2]), float(r[3]))
        for r in conn.execute(
            "select player_id, position, exp_points, p_play from predictions where run_id = %s",
            (run_id,),
        )
    }
    real = bt.actuals(conn, gid)
    rec = conn.execute(
        "select player_ids, formation from lineup_recommendations where run_id = %s", (run_id,)
    ).fetchone()
    lineup = bt.compare_lineups(conn, gid, preds, real)
    if lineup is not None and rec is not None:
        # El once recomendado de verdad (el que se envió), no el recalculado.
        lineup.recommended = sum(real.get(i, (0, False, False))[0] for i in rec[0])
        lineup.recommended_formation = str(rec[1])
    return GameweekEvaluation(
        number, gid, run_id, bt.metrics(preds, real), lineup, str(rec[1]) if rec else None
    )


@dataclass
class MarketAudit:
    audited: int
    buy_value_change: float | None  # media de lo recomendado comprar
    avoided_value_change: float | None  # media del resto del mercado ese día
    buy_points: float | None
    avoided_points: float | None


def _value_change(conn: Conn, pid: int, start: date, end: date) -> float | None:
    rows = conn.execute(
        "select (select value from player_value_daily where player_id = %s and value_date <= %s"
        "        order by value_date desc limit 1),"
        "       (select value from player_value_daily where player_id = %s and value_date <= %s"
        "        order by value_date desc limit 1)",
        (pid, start, pid, end),
    ).fetchone()
    if rows is None or not rows[0] or not rows[1]:
        return None
    return float(rows[1]) / float(rows[0]) - 1


def _points_since(conn: Conn, pid: int, start: date) -> int:
    row = conn.execute(
        "select coalesce(sum(p.points_final), 0) from player_gameweek p"
        " join gameweeks g on g.id = p.gameweek_id where p.player_id = %s"
        " and g.first_match_at::date > %s",
        (pid, start),
    ).fetchone()
    return int(row[0]) if row else 0


def audit_market(conn: Conn, today: date) -> MarketAudit:
    """Rellena `outcome` de las recomendaciones con al menos 7 días."""
    due = conn.execute(
        "select id, kind, player_id, report_date from recommendations"
        " where outcome is null and report_date <= %s",
        (today - timedelta(days=AUDIT_AFTER_DAYS),),
    ).fetchall()
    for rid, _, pid, day in due:
        change = _value_change(conn, int(pid), day, today)
        outcome = {
            "dias": (today - day).days,
            "variacion_valor": change,
            "puntos_desde": _points_since(conn, int(pid), day),
        }
        conn.execute(
            "update recommendations set outcome = %s, outcome_at = now() where id = %s",
            (Jsonb(outcome), rid),
        )
    buys = conn.execute(
        "select player_id, report_date, (outcome->>'variacion_valor')::float,"
        " (outcome->>'puntos_desde')::int from recommendations"
        " where kind = 'fichaje' and outcome is not null"
    ).fetchall()
    if not buys:
        return MarketAudit(len(due), None, None, None, None)
    avoided_changes, avoided_points = [], []
    for day in {b[1] for b in buys}:
        recommended = {b[0] for b in buys if b[1] == day}
        for (pid,) in conn.execute(
            "select player_id from market_snapshot where snapshot_date = %s", (day,)
        ):
            if pid in recommended:
                continue
            ch = _value_change(conn, int(pid), day, today)
            if ch is not None:
                avoided_changes.append(ch)
                avoided_points.append(_points_since(conn, int(pid), day))
    bc = [b[2] for b in buys if b[2] is not None]
    return MarketAudit(
        len(due),
        float(np.mean(bc)) if bc else None,
        float(np.mean(avoided_changes)) if avoided_changes else None,
        float(np.mean([b[3] for b in buys if b[3] is not None])),
        float(np.mean(avoided_points)) if avoided_points else None,
    )


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f} %".replace(".", ",")


def _num(x: float | None, nd: int = 2) -> str:
    return "—" if x is None else f"{x:.{nd}f}".replace(".", ",")


@dataclass
class WeeklyReport:
    message: str
    gameweek: GameweekEvaluation | None
    market: MarketAudit
    comparison: bt.Comparison | None


def build_weekly(
    conn: Conn, *, now: datetime | None = None, with_backtest: bool = True
) -> WeeklyReport:
    now = now or datetime.now(UTC)
    today = now.date()
    lines = ["📊 Evaluación semanal", ""]
    ge = evaluate_gameweek(conn)
    if ge is None:
        lines.append(
            "Jornada: aún no hay ninguna cerrada con predicciones guardadas antes del partido."
        )
    else:
        m = ge.metrics
        lines += [
            f"J{ge.number} (predicción guardada antes del primer partido)",
            f"- Puntos esperados: error medio {_num(m.mae)} · sesgo {m.bias:+.2f}".replace(".", ",")
            + f" · Spearman {_num(m.spearman)} ({m.n} jugadores)",
        ]
        if ge.lineup:
            lc = ge.lineup
            lines.append(
                f"- Once: recomendado {lc.recommended} pts · puesto {lc.played} pts"
                f" · los 11 más caros {lc.most_expensive} pts"
            )
    ma = audit_market(conn, today)
    lines += ["", f"Mercado (recomendaciones con ≥ {AUDIT_AFTER_DAYS} días)"]
    if ma.buy_value_change is None:
        lines.append("- Aún no hay fichajes recomendados con una semana de recorrido.")
    else:
        lines.append(
            f"- Fichajes recomendados: valor {_pct(ma.buy_value_change)},"
            f" {_num(ma.buy_points, 1)} pts"
            f" · resto del mercado: valor {_pct(ma.avoided_value_change)},"
            f" {_num(ma.avoided_points, 1)} pts"
        )
    cmp = None
    if with_backtest:
        report = bt.run_backtest(conn)
        cmp = report.compare()
        if cmp:
            verdict = "v1 sustituye a v0" if cmp.v1_better else "se mantiene v0"
            lines += [
                "",
                f"Modelo (backtest J{cmp.gameweeks[0]}–J{cmp.gameweeks[-1]}): error v0"
                f" {_num(cmp.mae_v0, 3)} · v1 {_num(cmp.mae_v1, 3)} · once v0 {cmp.lineup_v0}"
                f" · v1 {cmp.lineup_v1} pts → {verdict}",
            ]
    return WeeklyReport("\n".join(lines), ge, ma, cmp)


def save_weekly(conn: Conn, report: WeeklyReport) -> None:
    rows: list[tuple[str, int | None, str | None, dict[str, Any]]] = []
    if report.gameweek is not None:
        g = report.gameweek
        rows.append(
            ("jornada", g.gameweek_id, "v0",
             {"metricas": asdict(g.metrics), "once": asdict(g.lineup) if g.lineup else None,
              "run_id": g.run_id})
        )  # fmt: skip
    rows.append(("mercado", None, None, asdict(report.market)))
    if report.comparison is not None:
        rows.append(("backtest", None, "v0-v1", asdict(report.comparison)))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into evaluations (kind, gameweek_id, model, metrics, summary)"
            " values (%s, %s, %s, %s, %s)",
            [(k, g, m, Jsonb(x), report.message) for k, g, m, x in rows],
        )
