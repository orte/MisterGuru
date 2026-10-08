"""Job `market-report`: informe matinal de mercado y cláusulas (Fase 4).

Sin peticiones a ninguna fuente: usa lo que dejó snapshot-daily (plantillas,
saldo, mercado, feed) y calcula los puntos esperados de la próxima jornada con
el modelo v0. Cada recomendación se guarda en `recommendations` con sus números.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from mister_assistant.decide import market as mk
from mister_assistant.decide.balances import BalanceEstimate, estimate_balances
from mister_assistant.decide.lineup import Option, best_lineup
from mister_assistant.models.features import load_player_features, load_priors
from mister_assistant.models.points import forecast_points
from mister_assistant.models.value import forecast_all

log = logging.getLogger(__name__)

MADRID = ZoneInfo("Europe/Madrid")
Conn = psycopg.Connection[tuple[Any, ...]]
TOP_MARKET = 5
TOP_CLAUSES = 3
# El estimador de saldos rivales solo se usa si acierta con el propio (±).
MAX_OWN_BALANCE_ERROR = 3_000_000


@dataclass
class Recommendation:
    kind: str
    player_id: int
    message: str
    numbers: dict[str, Any]
    score: float | None = None
    manager_id: int | None = None


@dataclass
class MarketReport:
    report_date: date
    message: str
    recommendations: list[Recommendation] = field(default_factory=list)
    own_balance_error: int | None = None
    value_model: dict[str, Any] = field(default_factory=dict)


def _eur(x: float) -> str:
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000:
        return f"{sign}{x / 1_000_000:.2f} M€".replace(".", ",")
    return f"{sign}{x / 1000:.0f} k€"


def _pts(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def _pct(x: float) -> str:
    return f"{x * 100:+.1f} %".replace(".", ",")


def _next_gameweek_id(conn: Conn) -> int | None:
    row = conn.execute(
        "select g.id from gameweeks g join fixtures f on f.gameweek_id = g.id"
        " where g.status = 'unstarted' group by g.id"
        " having min(f.kickoff_at) > now() order by min(f.kickoff_at) limit 1"
    ).fetchone()
    return int(row[0]) if row else None


def _bid_ratios(conn: Conn) -> list[float]:
    """precio / valor de las compras ganadas al mercado del juego."""
    rows = conn.execute(
        "select e.price, coalesce((e.payload->>'value')::bigint, v.value)"
        " from league_events e left join lateral ("
        "  select value from player_value_daily pv where pv.player_id = e.player_id"
        "  and pv.value_date <= e.occurred_at::date order by pv.value_date desc limit 1"
        " ) v on true"
        " where e.category = 'transfer' and e.event_type = 'normal'"
        "  and e.from_manager_id is null and e.to_manager_id is not null and e.price > 0"
    ).fetchall()
    return [float(p) / float(v) for p, v in rows if v]


def build_market_report(conn: Conn, *, now: datetime | None = None) -> MarketReport:
    now = now or datetime.now(UTC)
    today = now.astimezone(MADRID).date()
    me = conn.execute(
        "select m.mister_manager_id, s.balance, s.max_bid, s.snapshot_date"
        " from managers m join manager_snapshot s on s.manager_id = m.mister_manager_id"
        " where m.is_me order by s.snapshot_date desc limit 1"
    ).fetchone()
    if me is None:
        raise ValueError("no hay snapshot del mánager propio: ejecuta antes snapshot-daily")
    me_id, balance, max_bid, snap_date = int(me[0]), int(me[1] or 0), int(me[2] or 0), me[3]

    # Puntos esperados de la próxima jornada para todos los jugadores.
    gw_id = _next_gameweek_id(conn)
    exp: dict[int, tuple[float, float, int]] = {}
    if gw_id is not None:
        priors = load_priors(conn)
        for f in load_player_features(conn, gw_id):
            pf = forecast_points(f, priors, simulations=200)
            exp[f.player_id] = (pf.exp_points, pf.minutes.p_play, f.position)
    names = {
        int(r[0]): (str(r[1]), r[2])
        for r in conn.execute(
            "select mister_player_id, coalesce(short_name, name), position from players"
        )
    }
    value_model, values = forecast_all(conn)

    def option(pid: int) -> Option | None:
        name, pos = names.get(pid, (str(pid), None))
        e = exp.get(pid)
        position = e[2] if e else pos
        if position not in (1, 2, 3, 4):
            return None
        return Option(pid, name, int(position), e[0] if e else 0.0, e[1] if e else 0.0)

    squad_rows = conn.execute(
        "select player_id, clause_value, clause_floor, market_value from squad_snapshot"
        " where manager_id = %s and snapshot_date = %s",
        (me_id, snap_date),
    ).fetchall()
    squad = [o for r in squad_rows if (o := option(int(r[0]))) is not None]
    my_ids = {o.player_id for o in squad}
    lineup = best_lineup(squad)

    recs: list[Recommendation] = []
    lines = [f"📈 Mercado {today:%d/%m} · saldo {_eur(balance)} · puja máx. {_eur(max_bid)}"]

    # -- fichajes del mercado ------------------------------------------------
    levels = mk.bid_levels(_bid_ratios(conn))
    market = conn.execute(
        "select player_id, market_value, sale_price, seller_manager_id from market_snapshot"
        " where snapshot_date = (select max(snapshot_date) from market_snapshot)"
    ).fetchall()
    valuations = []
    for pid, mval, price, seller in market:
        pid = int(pid)
        o = option(pid)
        if o is None or pid in my_ids or not mval:
            continue
        vf = values.get(pid)
        cand = mk.Candidate(
            pid, o.name, o.position, o.exp_points, o.p_play, int(mval),
            int(max(price or 0, mval)), vf.change_14d if vf else 0.0, seller,
        )  # fmt: skip
        valuations.append(mk.value(squad, cand, balance=balance))
    good = sorted((v for v in valuations if v.score > 0), key=lambda v: -v.score)[:TOP_MARKET]
    lines += ["", "🛒 Fichajes (puntos extra en 5 jornadas · valor a 14 días · pujas)"]
    if not good:
        lines.append("Nada en el mercado mejora tu once lo suficiente.")
    for v in good:
        c = v.candidate
        tight, likely, safe = levels.amounts(c.value) if c.seller_id is None else (c.price,) * 3
        out = ", ".join(names.get(p, (str(p), None))[0] for p in v.replaces) or "banquillo"
        cash = (
            ""
            if v.affordable_now
            else f" · ⚠️ faltan {_eur(v.needs_sales)} (vender antes de la jornada)"
        )
        bid = (
            f"puja {_eur(tight)} / {_eur(likely)} / {_eur(safe)}"
            if c.seller_id is None
            else f"lo vende un rival por {_eur(c.price)}"
        )
        msg = (
            f"- {c.name}: +{_pts(v.horizon_points)} pts (sale {out}) · {_pct(c.change_14d)}"
            f" · {bid}{cash}"
        )
        lines.append(msg)
        recs.append(
            Recommendation(
                "fichaje", c.player_id, msg,
                {"puntos_jornada": v.marginal_points, "puntos_horizonte": v.horizon_points,
                 "valor": c.value, "precio": c.price, "variacion_14d": c.change_14d,
                 "sobreprecio": v.premium, "pujas": [tight, likely, safe],
                 "muestras_pujas": levels.samples, "sustituye": v.replaces,
                 "cabe_con_saldo": v.affordable_now},
                v.score,
            )
        )  # fmt: skip

    # -- saldos rivales y cláusulas -------------------------------------------
    estimates = estimate_balances(conn)
    own = next((b for b in estimates if b.actual is not None), None)
    own_error = own.error if own else None
    reliable = own_error is not None and abs(own_error) <= MAX_OWN_BALANCE_ERROR
    # Un mánager sin plantilla (abandonó la liga) no puede clausular.
    rivals: list[BalanceEstimate] = [
        b for b in estimates if b.manager_id != me_id and (b.team_value or 0) > 0
    ]

    clause_rows = conn.execute(
        "select s.player_id, s.manager_id, s.clause_value, m.name from squad_snapshot s"
        " join managers m on m.mister_manager_id = s.manager_id"
        " where s.snapshot_date = %s and s.manager_id <> %s and s.clause_value is not null",
        (snap_date, me_id),
    ).fetchall()
    offers = []
    on_sale = {int(r[0]) for r in market}
    for pid, owner, clause, owner_name in clause_rows:
        if int(pid) in on_sale:
            continue  # si su dueño lo vende, ficharlo sale más barato que la cláusula
        o = option(int(pid))
        vf = values.get(int(pid))
        if o is None or vf is None or int(clause) > max_bid:
            continue
        cand = mk.Candidate(int(pid), o.name, o.position, o.exp_points, o.p_play, vf.value,
                            int(clause), vf.change_14d, int(owner))  # fmt: skip
        val = mk.value(squad, cand, balance=balance)
        if val.score > 0:
            offers.append((val, str(owner_name)))
    offers.sort(key=lambda t: -t[0].score)
    lines += ["", "🎯 Cláusulas a tu alcance"]
    if not offers:
        lines.append("Ninguna cláusula rival compensa ahora.")
    for val, owner_name in offers[:TOP_CLAUSES]:
        c = val.candidate
        cash = (
            "con saldo"
            if val.affordable_now
            else f"faltan {_eur(val.needs_sales)}: vender antes de la jornada"
        )
        msg = (
            f"- {c.name} ({owner_name}): cláusula {_eur(c.price)} (valor {_eur(c.value)})"
            f" · +{_pts(val.horizon_points)} pts · {cash}"
        )
        lines.append(msg)
        recs.append(
            Recommendation(
                "clausula_ofensiva", c.player_id, msg,
                {"clausula": c.price, "valor": c.value, "puntos_horizonte": val.horizon_points,
                 "sobreprecio": val.premium, "variacion_14d": c.change_14d,
                 "cabe_con_saldo": val.affordable_now},
                val.score, c.seller_id,
            )
        )  # fmt: skip

    lines += ["", "🛡️ Tus jugadores expuestos"]
    if not reliable:
        lines.append(
            "Sin estimación fiable de los saldos rivales"
            + (f" (con tu saldo falla en {_eur(own_error)})" if own_error is not None else "")
            + ". Mira tus cláusulas más bajas:"
        )
        cheap = sorted(squad_rows, key=lambda r: int(r[1] or 0))[:3]
        for pid, clause, _, _ in cheap:
            o = option(int(pid))
            if o is not None and clause:
                lines.append(f"- {o.name}: cláusula {_eur(int(clause))}")
    else:
        threat_list = [(b.manager_id, b.name, b.spendable()) for b in rivals]
        exposed = []
        for pid, clause, floor, _ in squad_rows:
            o = option(int(pid))
            if o is None or not clause or o.player_id not in lineup.player_ids:
                continue
            ex = mk.exposure(o, int(clause), int(floor or clause), squad, threat_list)
            if ex is not None and ex.marginal_loss >= 0.5:
                exposed.append(ex)
        if not exposed:
            lines.append("Ningún titular importante al alcance de los saldos estimados.")
        for ex in sorted(exposed, key=lambda e: -e.marginal_loss)[:TOP_CLAUSES]:
            who = ", ".join(t[1] for t in ex.threats[:3])
            if ex.raise_to is None:
                fix = "ni con la subida máxima queda fuera de su alcance"
            elif ex.raise_to.cost > balance:
                fix = (
                    f"subirla al escalón {ex.raise_to.step} costaría {_eur(ex.raise_to.cost)}"
                    f" y tienes {_eur(balance)}: no llega"
                )
            else:
                fix = (
                    f"subir al escalón {ex.raise_to.step} ({_eur(ex.raise_to.new_clause)})"
                    f" cuesta {_eur(ex.raise_to.cost)}"
                )
            msg = f"- {ex.name}: cláusula {_eur(ex.clause)} · al alcance de {who} · {fix}"
            lines.append(msg)
            recs.append(
                Recommendation(
                    "clausula_defensiva", ex.player_id, msg,
                    {"clausula": ex.clause, "perdida_puntos_jornada": ex.marginal_loss,
                     "amenazas": ex.threats[:5],
                     "subida": ex.raise_to.__dict__ if ex.raise_to else None},
                    ex.marginal_loss,
                )
            )  # fmt: skip

    # -- ventas ----------------------------------------------------------------
    keep = set(lineup.player_ids) | {
        o.player_id
        for o in sorted(
            (o for o in squad if o.player_id not in lineup.player_ids), key=lambda o: -o.exp_points
        )[:4]
    }
    sells = []
    for o in squad:
        vf = values.get(o.player_id)
        if o.player_id in keep or vf is None or vf.change_7d > -0.01:
            continue
        sells.append((o, vf))
    lines += ["", "💸 Ventas"]
    if not sells:
        lines.append("Nada que vender: los que no juegan no se esperan a la baja.")
    for o, vf in sorted(sells, key=lambda t: t[1].change_7d)[:3]:
        loss = round(vf.value * vf.change_7d)
        msg = (
            f"- {o.name}: valor {_eur(vf.value)}, se espera {_pct(vf.change_7d)}"
            f" en 7 días ({_eur(loss)})"
        )
        lines.append(msg)
        recs.append(
            Recommendation(
                "venta", o.player_id, msg,
                {"valor": vf.value, "variacion_7d": vf.change_7d, "puntos_jornada": o.exp_points},
                -loss / mk.EUR_PER_POINT,
            )
        )  # fmt: skip

    # -- movimientos de rivales ------------------------------------------------
    moves = conn.execute(
        "select e.event_type, e.price, coalesce(p.short_name, p.name), mf.name, mt.name"
        " from league_events e left join players p on p.mister_player_id = e.player_id"
        " left join managers mf on mf.mister_manager_id = e.from_manager_id"
        " left join managers mt on mt.mister_manager_id = e.to_manager_id"
        " where e.occurred_at >= %s and e.category = 'transfer'"
        "  and coalesce(e.to_manager_id, 0) <> %s and coalesce(e.from_manager_id, 0) <> %s"
        " order by e.occurred_at desc limit 8",
        (now - timedelta(hours=24), me_id, me_id),
    ).fetchall()
    if moves:
        lines += ["", "👀 Movimientos de rivales (24 h)"]
        for etype, price, pname, frm, to in moves:
            if etype == "clause":
                text = f"{to} se lleva a {pname} de {frm} por cláusula"
            elif to is None:
                text = f"{frm} vende a {pname} al mercado"
            elif frm is None:
                text = f"{to} ficha a {pname} del mercado"
            else:
                text = f"{to} ficha a {pname} de {frm}"
            lines.append(f"- {text} ({_eur(price or 0)})")

    vm = value_model.reports.get(14)
    if vm is not None:
        note = (
            f"modelo de valor 14 d: error {vm.mae_model:.3f} vs {vm.mae_zero:.3f} sin cambio"
            if vm.use_model
            else "modelo de valor 14 d: no mejora a «sin cambio», se usa 0"
        )
        lines += ["", note]
    return MarketReport(
        today, "\n".join(lines), recs, own_error,
        {str(h): r.__dict__ for h, r in value_model.reports.items()},
    )  # fmt: skip


def save_recommendations(conn: Conn, report: MarketReport) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            "insert into recommendations (report_date, kind, player_id, manager_id, rank, score,"
            "  numbers, message) values (%s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (report.report_date, r.kind, r.player_id, r.manager_id, i + 1,
                 r.score, Jsonb(r.numbers), r.message)
                for i, r in enumerate(report.recommendations)
            ],
        )  # fmt: skip
    return len(report.recommendations)
