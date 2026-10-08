"""Estimación de saldos (los de los rivales están ocultos en la liga).

saldo ≈ saldo inicial
        + Σ jornadas (puntos × 100.000 € + (puesto en la jornada − 1) × 200.000 €)
        + Σ ventas − Σ compras (traspasos y cláusulas de league_events)

Lo que no se ve (subidas de cláusula, ingresos o gastos que no salen en el feed)
queda como error. Se valida con el saldo propio, que sí se conoce: el error
propio da la incertidumbre que se aplica a los rivales.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import psycopg

Conn = psycopg.Connection[tuple[Any, ...]]

STARTING_BALANCE = 50_000_000
EUR_PER_POINT = 100_000
EUR_PER_RANK_STEP = 200_000  # (puesto − 1) × 200.000 €; el 1º cobra 0


@dataclass
class BalanceEstimate:
    manager_id: int
    name: str
    estimated: int
    points_bonus: int
    rank_bonus: int
    sales: int
    purchases: int
    gameweeks: int
    team_value: int | None = None
    actual: int | None = None  # solo el propio
    details: dict[str, int] = field(default_factory=dict)

    @property
    def error(self) -> int | None:
        return None if self.actual is None else self.estimated - self.actual

    def spendable(self, debt_share: float = 0.25) -> int:
        """Lo que podría gastar ya: saldo + deuda permitida (25 % del valor del equipo)."""
        return self.estimated + int(debt_share * (self.team_value or 0))


def gameweek_points(conn: Conn) -> dict[int, dict[int, int]]:
    """Puntos por mánager y jornada cerrada, de la última respuesta de /ajax/sw/users."""
    out: dict[int, dict[int, int]] = defaultdict(dict)
    rows = conn.execute(
        "select distinct on (params->>'id') (params->>'id')::int,"
        " body_json->'data'->'userGameWeeks'"
        " from raw_responses where source = 'mister' and route = 'user'"
        " order by params->>'id', captured_at desc"
    ).fetchall()
    finished = {
        int(r[0]) for r in conn.execute("select id from gameweeks where status = 'finished'")
    }
    for manager_id, ugw in rows:
        for gw_id, data in (ugw or {}).items():
            if int(gw_id) in finished and isinstance(data, dict):
                out[int(manager_id)][int(gw_id)] = int(data.get("points") or 0)
    return out


def rank_bonus(points_by_manager: dict[int, int], managers: list[int]) -> dict[int, int]:
    """Premio por puesto en una jornada. Empates: puesto compartido (1, 2, 2, 4…)."""
    pts = {m: points_by_manager.get(m, 0) for m in managers}
    ordered = sorted(pts.values(), reverse=True)
    return {m: (ordered.index(p)) * EUR_PER_RANK_STEP for m, p in pts.items()}


def estimate_balances(conn: Conn) -> list[BalanceEstimate]:
    managers = conn.execute(
        "select m.mister_manager_id, m.name, m.is_me, s.balance, s.team_value"
        " from managers m left join lateral ("
        "  select balance, team_value from manager_snapshot ms"
        "  where ms.manager_id = m.mister_manager_id order by snapshot_date desc limit 1"
        " ) s on true"
    ).fetchall()
    ids = [int(m[0]) for m in managers]
    points = gameweek_points(conn)
    gameweeks = sorted({gw for per in points.values() for gw in per})
    rank_total: dict[int, int] = defaultdict(int)
    for gw in gameweeks:
        for m, bonus in rank_bonus({m: points[m].get(gw, 0) for m in ids}, ids).items():
            rank_total[m] += bonus
    flows: dict[int, dict[str, int]] = defaultdict(lambda: {"sales": 0, "purchases": 0})
    for frm, to, price in conn.execute(
        "select from_manager_id, to_manager_id, coalesce(price, 0) from league_events"
        " where category = 'transfer'"
    ):
        if frm is not None:
            flows[int(frm)]["sales"] += int(price)
        if to is not None:
            flows[int(to)]["purchases"] += int(price)

    out: list[BalanceEstimate] = []
    for mid, name, is_me, balance, team_value in managers:
        mid = int(mid)
        pbonus = EUR_PER_POINT * sum(points.get(mid, {}).values())
        f = flows[mid]
        est = STARTING_BALANCE + pbonus + rank_total[mid] + f["sales"] - f["purchases"]
        out.append(
            BalanceEstimate(
                manager_id=mid,
                name=str(name),
                estimated=est,
                points_bonus=pbonus,
                rank_bonus=rank_total[mid],
                sales=f["sales"],
                purchases=f["purchases"],
                gameweeks=len(points.get(mid, {})),
                team_value=int(team_value) if team_value is not None else None,
                actual=int(balance) if is_me and balance is not None else None,
            )
        )
    return out
