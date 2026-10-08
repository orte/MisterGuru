"""Estimación de saldos (los de los rivales están ocultos en la liga).

saldo ≈ 50 M€ − valor de la plantilla inicial (los 15 jugadores iniciales se
        descuentan del saldo de partida)
        + Σ jornadas (puntos × 100.000 € + (puesto en la jornada − 1) × 200.000 €)
        + Σ ventas − Σ compras (traspasos y cláusulas de league_events)

Lo que no se ve (subidas de cláusula, ingresos o gastos que no salen en el feed)
queda como error. Se valida con el saldo propio, que sí se conoce: el error
propio da la incertidumbre que se aplica a los rivales.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import psycopg

Conn = psycopg.Connection[tuple[Any, ...]]

STARTING_BALANCE = 50_000_000
STARTING_PLAYERS = 15
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
    initial_value: int = 0
    initial_players: int = 0  # identificados (de 15)
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


def initial_squads(conn: Conn) -> tuple[dict[int, list[int]], dict[int, int]]:
    """Jugadores iniciales de cada mánager y su valor al empezar la temporada.

    Inicial = su primer traspaso en la liga es una venta de ese mánager, o lo tiene
    ahora y nunca se ha movido. El valor es el del día anterior al primer traspaso
    de la liga. Los iniciales vendidos sin dejar rastro (nunca jugaron ni volvieron
    a tener dueño) no se ven, pero su compra y su venta se compensan casi del todo.
    """
    first = conn.execute("select min(occurred_at)::date from league_events").fetchone()
    if first is None or first[0] is None:
        return {}, {}
    start = first[0] - timedelta(days=1)
    squads: dict[int, list[int]] = defaultdict(list)
    seen: set[int] = set()
    for pid, frm in conn.execute(
        "select distinct on (player_id) player_id, from_manager_id from league_events"
        " where category = 'transfer' and player_id is not null"
        " order by player_id, occurred_at"
    ):
        seen.add(int(pid))
        if frm is not None:
            squads[int(frm)].append(int(pid))
    for pid, mid in conn.execute(
        "select player_id, manager_id from squad_snapshot"
        " where snapshot_date = (select max(snapshot_date) from squad_snapshot)"
    ):
        if int(pid) not in seen:
            squads[int(mid)].append(int(pid))
    values = {
        int(r[0]): int(r[1])
        for r in conn.execute(
            "select distinct on (player_id) player_id, value from player_value_daily"
            " where value_date <= %s order by player_id, value_date desc",
            (start,),
        )
    }
    totals = {m: sum(values.get(p, 0) for p in ps) for m, ps in squads.items()}
    return dict(squads), totals


def estimate_balances(conn: Conn) -> list[BalanceEstimate]:
    managers = conn.execute(
        "select m.mister_manager_id, m.name, m.is_me, s.balance, s.team_value"
        " from managers m left join lateral ("
        "  select balance, team_value from manager_snapshot ms"
        "  where ms.manager_id = m.mister_manager_id order by snapshot_date desc limit 1"
        " ) s on true"
    ).fetchall()
    ids = [int(m[0]) for m in managers]
    initial, initial_value = initial_squads(conn)
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
        start = STARTING_BALANCE - initial_value.get(mid, 0)
        est = start + pbonus + rank_total[mid] + f["sales"] - f["purchases"]
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
                initial_value=initial_value.get(mid, 0),
                initial_players=len(initial.get(mid, [])),
                team_value=int(team_value) if team_value is not None else None,
                actual=int(balance) if is_me and balance is not None else None,
            )
        )
    return out
