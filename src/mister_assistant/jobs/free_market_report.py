"""Job `free-market-report`: ¿merece la pena pujar hoy por algún jugador libre?

Solo jugadores libres: los que vende el propio juego en el mercado (no los que
pone a la venta un rival) y cuyo plazo sigue abierto. Sin peticiones: usa el
mercado que guardó snapshot-daily y las predicciones de la próxima jornada.

Un jugador merece una puja por uno de dos motivos, que se muestran por separado:

- **Para puntuar**: mejora el once recomendado en al menos 1,5 puntos en 5
  jornadas (0,3 por jornada).
- **Para revalorizar**: su subida de valor esperada a 14 días supera en al menos
  250.000 € al sobreprecio de la puja «probable» más lo que se pierde al
  revender (2,5 %). Las previsiones de valor son orientativas.

Cada candidato lleva las pujas ajustada / probable / segura (según las compras
ganadas al mercado del juego en la liga) y si cabe en la caja.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from mister_assistant.agent.context import POSITION, Snapshot, load_snapshot, value_forecasts
from mister_assistant.decide import market as mk
from mister_assistant.jobs.market_report import _bid_ratios

MADRID = ZoneInfo("Europe/Madrid")
Conn = psycopg.Connection[tuple[Any, ...]]
MIN_HORIZON_POINTS = 1.5
MIN_SPECULATION_EUR = 250_000
MAX_SHOWN = 4
_DAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


@dataclass(frozen=True)
class FreePlayer:
    player_id: int
    name: str
    position: int
    team: str | None
    value: int
    ends_at: datetime | None
    horizon_points: float
    replaces: list[int]
    change_14d: float
    bids: tuple[int, int, int]
    speculation_eur: int  # subida esperada − sobreprecio (puja probable) − reventa
    affordable: bool  # la puja probable cabe en la puja máxima
    needs_sales: int  # lo que habría que vender para no empezar la jornada en negativo

    @property
    def for_points(self) -> bool:
        return self.horizon_points >= MIN_HORIZON_POINTS

    @property
    def for_value(self) -> bool:
        return self.speculation_eur >= MIN_SPECULATION_EUR


@dataclass
class FreeMarketReport:
    report_date: date
    message: str
    worth_it: list[FreePlayer] = field(default_factory=list)
    free_count: int = 0
    run_id: int | None = None


def _eur(x: float) -> str:
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000:
        return f"{sign}{x / 1_000_000:.2f} M€".replace(".", ",")
    return f"{sign}{x / 1000:.0f} k€"


def _when(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    local = dt.astimezone(MADRID)
    return f"{_DAYS[local.weekday()]} {local:%d/%m %H:%M}"


def free_players(conn: Conn, snap: Snapshot, now: datetime) -> list[FreePlayer]:
    rows = conn.execute(
        "select player_id, market_value, sale_ends_at from market_snapshot"
        " where snapshot_date = (select max(snapshot_date) from market_snapshot)"
        " and seller_manager_id is null and (sale_ends_at is null or sale_ends_at > %s)",
        (now,),
    ).fetchall()
    levels = mk.bid_levels(_bid_ratios(conn))
    values = value_forecasts(conn)
    squad = snap.squad_options()
    out = []
    for pid, mval, ends in rows:
        pid = int(pid)
        opt = snap.option(pid)
        if opt is None or pid in snap.squad or not mval:
            continue
        vf = values.get(pid)
        change = vf.change_14d if vf else 0.0
        bids = levels.amounts(int(mval))
        cand = mk.Candidate(pid, opt.name, opt.position, opt.exp_points, opt.p_play, int(mval),
                            bids[1], change)  # fmt: skip
        v = mk.value(squad, cand, balance=snap.balance)
        speculation = v.expected_value_change - v.premium
        out.append(
            FreePlayer(
                player_id=pid,
                name=opt.name,
                position=opt.position,
                team=snap.names.get(pid, ("", None, None))[2],
                value=int(mval),
                ends_at=ends,
                horizon_points=v.horizon_points,
                replaces=v.replaces,
                change_14d=change,
                bids=bids,
                speculation_eur=speculation,
                affordable=bids[1] <= snap.max_bid,
                needs_sales=max(0, bids[1] - snap.balance),
            )
        )
    return out


def build_free_market_report(conn: Conn, *, now: datetime | None = None) -> FreeMarketReport:
    now = now or datetime.now(UTC)
    today = now.astimezone(MADRID).date()
    snap = load_snapshot(conn, trigger="mercado")
    players = free_players(conn, snap, now)
    worth = [p for p in players if (p.for_points or p.for_value) and p.affordable]
    too_expensive = [p for p in players if (p.for_points or p.for_value) and not p.affordable]
    closes = min((p.ends_at for p in players if p.ends_at), default=None)

    if worth:
        head = f"🟢 Mercado libre {today:%d/%m}: sí, merece la pena pujar"
    else:
        head = f"🔴 Mercado libre {today:%d/%m}: no merece la pena pujar hoy"
    lines = [
        head,
        f"{len(players)} jugadores libres · primer cierre {_when(closes)}"
        f" · saldo {_eur(snap.balance)} · puja máx. {_eur(snap.max_bid)}",
    ]

    def line(p: FreePlayer) -> str:
        out = ", ".join(snap.names.get(r, (str(r), None, None))[0] for r in p.replaces)
        cash = "✅ cabe en el saldo" if p.needs_sales == 0 else (
            f"⚠️ faltan {_eur(p.needs_sales)}: vende antes de la jornada"
        )  # fmt: skip
        return (
            f"- {p.name} ({POSITION.get(p.position)}, {p.team}) · valor {_eur(p.value)}"
            f" · cierra {_when(p.ends_at)}\n"
            f"  puja {_eur(p.bids[0])} / {_eur(p.bids[1])} / {_eur(p.bids[2])} · {cash}"
            + (f"\n  +{p.horizon_points:.1f} pts en 5 jornadas (sale {out})".replace(".", ",")
               if p.for_points else "")
            + (f"\n  revalorización esperada neta {_eur(p.speculation_eur)}"
               f" ({p.change_14d * 100:+.0f} % en 14 días)" if p.for_value else "")
        )  # fmt: skip

    points = sorted((p for p in worth if p.for_points), key=lambda p: -p.horizon_points)
    value = sorted(
        (p for p in worth if p.for_value and not p.for_points), key=lambda p: -p.speculation_eur
    )
    if points:
        lines += ["", "⚽ Para puntuar"] + [line(p) for p in points[:MAX_SHOWN]]
        shared = set.intersection(*(set(p.replaces) for p in points[:MAX_SHOWN]))
        if len(points) > 1 and shared:
            who = ", ".join(snap.names.get(r, (str(r), None, None))[0] for r in shared)
            lines.append(f"  Son alternativas: todos entrarían por {who}; con uno basta.")
    if value:
        lines += ["", "📈 Para revalorizar (previsión orientativa)"]
        lines += [line(p) for p in value[:MAX_SHOWN]]
    if not worth:
        best = max(players, key=lambda p: p.horizon_points, default=None)
        if best is not None and best.horizon_points > 0:
            lines += [
                "",
                f"El que más se acerca: {best.name}, +{best.horizon_points:.1f} pts en 5 jornadas"
                f" (hace falta {MIN_HORIZON_POINTS:.1f})".replace(".", ","),
            ]
        else:
            lines += ["", "Ninguno entra en tu once ni se espera que suba lo suficiente."]
    if too_expensive:
        names = ", ".join(p.name for p in too_expensive[:3])
        lines += ["", f"Interesarían pero no llegas ni endeudándote: {names}"]
    lines += [
        "",
        "Recuerda: con saldo negativo al empezar la jornada se puntúa 0.",
        f"Predicciones del run {snap.run_id} (J{snap.gameweek_number}).",
    ]
    return FreeMarketReport(today, "\n".join(lines), points + value, len(players), snap.run_id)


def already_sent(conn: Conn, report_date: date) -> bool:
    row = conn.execute(
        "select 1 from recommendations where kind = 'puja' and report_date = %s limit 1",
        (report_date,),
    ).fetchone()
    return row is not None


def save(conn: Conn, report: FreeMarketReport) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            "insert into recommendations (report_date, kind, player_id, rank, score, numbers,"
            " message) values (%s, 'puja', %s, %s, %s, %s, %s)",
            [
                (report.report_date, p.player_id, i + 1,
                 p.horizon_points if p.for_points else p.speculation_eur / mk.EUR_PER_POINT,
                 Jsonb({"motivo": "puntos" if p.for_points else "revalorizacion",
                        "valor": p.value, "pujas": list(p.bids),
                        "puntos_5_jornadas": p.horizon_points, "sustituye": p.replaces,
                        "variacion_14d": p.change_14d, "revalorizacion_neta": p.speculation_eur,
                        "falta_vender": p.needs_sales, "cierra": str(p.ends_at),
                        "run_id": report.run_id}),
                 report.message)
                for i, p in enumerate(report.worth_it)
            ],
        )  # fmt: skip
    return len(report.worth_it)
