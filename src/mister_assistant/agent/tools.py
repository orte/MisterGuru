"""Herramientas del agente. Todas consultan la BD; ninguna toca Mister.

La única escritura es `registrar_ajuste_disponibilidad`, en nuestra propia tabla
`availability_overrides` (triaje de noticias). Cada resultado lleva `run_id` o la
tabla de la que sale, para que las cifras de la respuesta sean trazables.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import psycopg

from mister_assistant.agent.context import POSITION, Snapshot, load_snapshot, value_forecasts
from mister_assistant.decide import market as mk
from mister_assistant.decide.balances import estimate_balances
from mister_assistant.decide.lineup import FORMATIONS, Option, best_lineup, recommend
from mister_assistant.identity.store import search_players
from mister_assistant.jobs.gameweek_report import _DAYS, MADRID

Conn = psycopg.Connection[tuple[Any, ...]]


class ToolError(Exception):
    """Error que se devuelve al modelo como tool_result con is_error."""


def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


def _nullable(schema: dict[str, Any], description: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"anyOf": [schema, {"type": "null"}]}
    if description:
        out["description"] = description
    return out


_ID = {"type": "integer", "description": "mister_player_id (sácalo de buscar_jugador)"}
_IDS = {"type": "array", "items": {"type": "integer"}, "description": "mister_player_id"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "buscar_jugador",
        "description": "Busca jugadores de Mister por nombre (tolera tildes y nombres cortos)."
        " Devuelve id, nombre, equipo y posición. Úsala antes de cualquier otra si solo"
        " tienes el nombre.",
        "input_schema": _schema(
            {"nombre": {"type": "string"}, "equipo": _nullable({"type": "string"})}
        ),
    },
    {
        "name": "ficha_jugador",
        "description": "Todo lo que sabemos de un jugador para la próxima jornada: puntos"
        " esperados con suelo y techo, P(titular) y P(jugar) y de dónde salen, valor de"
        " mercado y su previsión a 7 y 14 días, dueño y cláusula, puntos de las últimas"
        " jornadas y ajustes por noticias vigentes.",
        "input_schema": _schema({"player_id": _ID}),
    },
    {
        "name": "comparar_jugadores",
        "description": "Compara varios jugadores (2 a 6) en puntos esperados, P(jugar),"
        " valor y previsión de valor.",
        "input_schema": _schema({"player_ids": _IDS}),
    },
    {
        "name": "once_recomendado",
        "description": "Once óptimo de mi plantilla para la próxima jornada (solo formaciones"
        " gratuitas), con dudas y recambios, el banquillo y quién se queda fuera.",
        "input_schema": _schema({}),
    },
    {
        "name": "simular_once",
        "description": "Puntos esperados de un once concreto de mi plantilla (11 ids) y si la"
        " formación es válida, frente al recomendado.",
        "input_schema": _schema({"player_ids": _IDS}),
    },
    {
        "name": "valorar_fichaje",
        "description": "Valora fichar a un jugador: puntos extra en el once en 5 jornadas, a"
        " quién sustituye, variación de valor esperada, sobreprecio, pujas recomendadas"
        " (ajustada, probable, segura) y si cabe en el saldo. Si está en otra plantilla, se"
        " valora por su cláusula. `precio` opcional para valorar un precio concreto.",
        "input_schema": _schema({"player_id": _ID, "precio": _nullable({"type": "integer"})}),
    },
    {
        "name": "analizar_venta",
        "description": "¿Vender a un jugador de mi plantilla? Puntos que pierde el once sin él,"
        " valor actual y previsto a 7 y 14 días, y si es titular o banquillo.",
        "input_schema": _schema({"player_id": _ID}),
    },
    {
        "name": "estado_rivales",
        "description": "Clasificación, valor de equipo y saldo estimado de cada rival (los"
        " saldos rivales están ocultos y son estimaciones), y qué jugadores míos podrían"
        " clausularme.",
        "input_schema": _schema({}),
    },
    {
        "name": "mercado_hoy",
        "description": "Jugadores en el mercado hoy ordenados por valoración como fichaje.",
        "input_schema": _schema({}),
    },
    {
        "name": "registrar_ajuste_disponibilidad",
        "description": "Registra lo que dice una noticia (rueda de prensa, parte médico) sobre"
        " la disponibilidad de un jugador. Lo usa el modelo de minutos hasta `hasta`."
        " Solo con una fuente y una frase concreta de la noticia; nunca por suposición.",
        "input_schema": _schema(
            {
                "player_id": _ID,
                "estado": {"type": "string", "enum": ["baja", "duda", "disponible"]},
                "p_jugar": _nullable(
                    {"type": "number"}, "0-1 solo si la noticia permite estimarla; si no, null"
                ),
                "hasta": {"type": "string", "format": "date", "description": "último día vigente"},
                "fuente": {"type": "string", "description": "medio o persona que lo dice"},
                "fecha_fuente": {
                    "type": "string",
                    "format": "date",
                    "description": "de la noticia",
                },
                "cita": {"type": "string", "description": "frase literal que lo justifica"},
            }
        ),
    },
    {
        "name": "ajustes_vigentes",
        "description": "Ajustes de disponibilidad por noticias que están vigentes.",
        "input_schema": _schema({}),
    },
]
for _t in TOOLS:
    _t["strict"] = True


def _pred(snap: Snapshot, pid: int) -> dict[str, Any] | None:
    pr = snap.predictions.get(pid)
    if pr is None:
        return None
    return {
        "puntos_esperados": pr.exp_points,
        "suelo_p20": pr.p20,
        "techo_p80": pr.p80,
        "p_titular": round(pr.p_start, 3),
        "p_jugar": round(pr.p_play, 3),
        "fuente_minutos": pr.components.get("fuente_minutos"),
        "p_victoria_equipo": pr.components.get("p_victoria"),
    }


def _name(snap: Snapshot, pid: int) -> dict[str, Any]:
    name, pos, team = snap.names.get(pid, (str(pid), None, None))
    return {"id": pid, "nombre": name, "equipo": team, "posicion": POSITION.get(pos or 0)}


class Toolbox:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn
        self._snap: Snapshot | None = None

    @property
    def snap(self) -> Snapshot:
        if self._snap is None:
            self._snap = load_snapshot(self.conn)
        return self._snap

    def handlers(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {
            "buscar_jugador": self.buscar_jugador,
            "ficha_jugador": self.ficha_jugador,
            "comparar_jugadores": self.comparar_jugadores,
            "once_recomendado": self.once_recomendado,
            "simular_once": self.simular_once,
            "valorar_fichaje": self.valorar_fichaje,
            "analizar_venta": self.analizar_venta,
            "estado_rivales": self.estado_rivales,
            "mercado_hoy": self.mercado_hoy,
            "registrar_ajuste_disponibilidad": self.registrar_ajuste,
            "ajustes_vigentes": self.ajustes_vigentes,
        }

    def run(self, name: str, args: dict[str, Any]) -> str:
        handler = self.handlers().get(name)
        if handler is None:
            raise ToolError(f"herramienta desconocida: {name}")
        out = handler(**args)
        return json.dumps(out, ensure_ascii=False, default=str)

    # -- base ---------------------------------------------------------------------

    def _base(self) -> dict[str, Any]:
        s = self.snap
        kick = s.first_kickoff.astimezone(MADRID)
        return {
            "run_id": s.run_id,
            "jornada": s.gameweek_number,
            "primer_partido_hora_madrid": f"{_DAYS[kick.weekday()]} {kick:%d/%m %H:%M}",
        }

    def buscar_jugador(self, nombre: str, equipo: str | None) -> dict[str, Any]:
        hits = search_players(self.conn, nombre, equipo, limit=6)
        return {
            "resultados": [
                {"id": pid, "nombre": n, "equipo": t, "posicion": POSITION.get(pos or 0),
                 "parecido": round(score)}
                for pid, n, t, pos, score in hits if score >= 50
            ]
        }  # fmt: skip

    def ficha_jugador(self, player_id: int) -> dict[str, Any]:
        s = self.snap
        if player_id not in s.names:
            raise ToolError(f"no existe el jugador {player_id}")
        vf = value_forecasts(self.conn).get(player_id)
        owner = self.conn.execute(
            "select m.name, sq.clause_value, m.is_me from squad_snapshot sq"
            " join managers m on m.mister_manager_id = sq.manager_id"
            " where sq.player_id = %s and sq.snapshot_date = %s",
            (player_id, s.snapshot_date),
        ).fetchone()
        recent = self.conn.execute(
            "select g.number, p.points_final, p.minutes, p.sub_in_minute is null"
            " from player_gameweek p join gameweeks g on g.id = p.gameweek_id"
            " where p.player_id = %s order by g.number desc limit 5",
            (player_id,),
        ).fetchall()
        overrides = self.conn.execute(
            "select status, p_play, valid_until, source, source_date, quote"
            " from availability_overrides where player_id = %s and active"
            " and valid_until >= current_date order by source_date desc",
            (player_id,),
        ).fetchall()
        return {
            **self._base(),
            "jugador": _name(s, player_id),
            "prediccion": _pred(s, player_id)
            or "su equipo no juega la próxima jornada o no hay datos",
            "valor": {
                "actual": vf.value if vf else None,
                "variacion_7d": round(vf.change_7d, 4) if vf else None,
                "variacion_14d": round(vf.change_14d, 4) if vf else None,
                "variacion_7d_eur": round(vf.value * vf.change_7d) if vf else None,
                "variacion_14d_eur": round(vf.value * vf.change_14d) if vf else None,
            },
            "dueno": {"nombre": owner[0], "clausula": owner[1], "es_mio": bool(owner[2])}
            if owner
            else "libre (sin dueño en la liga)",
            "ultimas_jornadas": [
                {"jornada": n, "puntos": p, "minutos": m, "titular": t} for n, p, m, t in recent
            ],
            "ajustes_noticias": [
                {"estado": st, "p_jugar": pp, "hasta": str(vu), "fuente": src,
                 "fecha": str(sd), "cita": q}
                for st, pp, vu, src, sd, q in overrides
            ],
        }  # fmt: skip

    def comparar_jugadores(self, player_ids: list[int]) -> dict[str, Any]:
        if not 2 <= len(player_ids) <= 6:
            raise ToolError("compara entre 2 y 6 jugadores")
        values = value_forecasts(self.conn)
        rows = []
        for pid in player_ids:
            vf = values.get(pid)
            rows.append(
                {**_name(self.snap, pid), "prediccion": _pred(self.snap, pid),
                 "valor": vf.value if vf else None,
                 "variacion_14d": round(vf.change_14d, 4) if vf else None}
            )  # fmt: skip
        return {**self._base(), "jugadores": rows}

    def once_recomendado(self) -> dict[str, Any]:
        s = self.snap
        squad = s.squad_options()
        lineup = recommend(squad, FORMATIONS)
        bench = sorted(
            (o for o in squad if o.player_id not in lineup.player_ids), key=lambda o: -o.exp_points
        )
        return {
            **self._base(),
            "formacion": lineup.formation,
            "puntos_esperados_once": round(lineup.expected_points, 2),
            "huecos": lineup.empty_slots,
            "once": [
                {**_name(s, o.player_id), "puntos_esperados": o.exp_points, "p_jugar": o.p_play}
                for o in lineup.players
            ],
            "dudas": [
                {"jugador": _name(s, f.player_id), "p_jugar": round(f.p_play, 3),
                 "recambio": _name(s, f.replacement_id) if f.replacement_id else None,
                 "puntos_que_se_pierden": f.cost}
                for f in lineup.fragile
            ],
            "banquillo": [
                {**_name(s, o.player_id), "puntos_esperados": o.exp_points, "p_jugar": o.p_play}
                for o in bench
            ],
        }  # fmt: skip

    def simular_once(self, player_ids: list[int]) -> dict[str, Any]:
        s = self.snap
        squad = {o.player_id: o for o in s.squad_options()}
        missing = [p for p in player_ids if p not in squad]
        if missing:
            raise ToolError(f"no están en tu plantilla: {missing}")
        if len(set(player_ids)) != 11:
            raise ToolError("un once tiene 11 jugadores distintos")
        picked = [squad[p] for p in player_ids]
        counts = {pos: sum(1 for o in picked if o.position == pos) for pos in (1, 2, 3, 4)}
        formation = f"{counts[2]}-{counts[3]}-{counts[4]}"
        valid = counts[1] == 1 and formation in FORMATIONS
        best = best_lineup(list(squad.values()), FORMATIONS)
        total = sum(o.exp_points for o in picked)
        return {
            **self._base(),
            "formacion": formation,
            "valida": valid,
            "motivo": None
            if valid
            else "solo 1 portero y formaciones gratuitas: " + ", ".join(FORMATIONS),
            "puntos_esperados": round(total, 2),
            "recomendado": round(best.expected_points, 2),
            "diferencia": round(total - best.expected_points, 2),
        }

    def _candidate(self, player_id: int, price: int | None) -> tuple[mk.Candidate, str]:
        s = self.snap
        opt = s.option(player_id)
        if opt is None:
            raise ToolError(f"no existe el jugador {player_id}")
        vf = value_forecasts(self.conn).get(player_id)
        if vf is None:
            raise ToolError("no hay valor de mercado de este jugador")
        sale = self.conn.execute(
            "select sale_price, seller_manager_id from market_snapshot where player_id = %s"
            " and snapshot_date = (select max(snapshot_date) from market_snapshot)",
            (player_id,),
        ).fetchone()
        owner = self.conn.execute(
            "select clause_value, manager_id from squad_snapshot where player_id = %s"
            " and snapshot_date = %s",
            (player_id, s.snapshot_date),
        ).fetchone()
        if price is not None:
            how, p = "precio indicado", price
        elif sale is not None:
            how, p = "en el mercado", int(max(sale[0] or 0, vf.value))
        elif owner is not None and owner[1] != s.me_id:
            how, p = "cláusula", int(owner[0])
        else:
            how, p = "valor de mercado (no está en venta)", vf.value
        seller = int(sale[1]) if sale and sale[1] else (int(owner[1]) if owner else None)
        cand = mk.Candidate(
            player_id, opt.name, opt.position, opt.exp_points, opt.p_play, vf.value, p,
            vf.change_14d, seller,
        )  # fmt: skip
        return cand, how

    def valorar_fichaje(self, player_id: int, precio: int | None) -> dict[str, Any]:
        s = self.snap
        if player_id in s.squad:
            raise ToolError("ya está en tu plantilla: usa analizar_venta")
        cand, how = self._candidate(player_id, precio)
        v = mk.value(s.squad_options(), cand, balance=s.balance)
        from mister_assistant.jobs.market_report import _bid_ratios

        levels = mk.bid_levels(_bid_ratios(self.conn))
        bids = levels.amounts(cand.value)
        return {
            **self._base(),
            "jugador": _name(s, player_id),
            "precio": cand.price,
            "precio_segun": how,
            "valor": cand.value,
            "puntos_extra_jornada": v.marginal_points,
            "puntos_extra_5_jornadas": v.horizon_points,
            "sustituye_a": [_name(s, p) for p in v.replaces],
            "variacion_valor_14d_eur": v.expected_value_change,
            "sobreprecio_eur": v.premium,
            "puntuacion": v.score,
            "pujas": {"ajustada": bids[0], "probable": bids[1], "segura": bids[2],
                      "multiplicadores": [levels.tight, levels.likely, levels.safe],
                      "compras_en_el_historico": levels.samples},
            "saldo": s.balance,
            "puja_maxima": s.max_bid,
            "cabe_con_saldo": v.affordable_now,
            "falta_vender_eur": v.needs_sales,
            "regla": "con saldo negativo al empezar la jornada se puntúa 0",
        }  # fmt: skip

    def analizar_venta(self, player_id: int) -> dict[str, Any]:
        s = self.snap
        if player_id not in s.squad:
            raise ToolError("no está en tu plantilla")
        squad = s.squad_options()
        with_him = best_lineup(squad, FORMATIONS)
        without = best_lineup(squad, FORMATIONS, exclude=frozenset({player_id}))
        vf = value_forecasts(self.conn).get(player_id)
        clause = self.conn.execute(
            "select clause_value from squad_snapshot where player_id = %s and snapshot_date = %s",
            (player_id, s.snapshot_date),
        ).fetchone()
        lost = round(with_him.expected_points - without.expected_points, 2)
        return {
            **self._base(),
            "jugador": _name(s, player_id),
            "prediccion": _pred(s, player_id),
            "es_titular_recomendado": player_id in with_him.player_ids,
            "puntos_que_pierde_el_once_por_jornada": lost,
            "puntos_que_pierde_el_once_5_jornadas": round(lost * mk.HORIZON_GAMEWEEKS, 2),
            "puntos_5_jornadas_en_eur_de_bonificacion": round(
                lost * mk.HORIZON_GAMEWEEKS * mk.EUR_PER_POINT
            ),
            "valor": vf.value if vf else None,
            "variacion_7d": round(vf.change_7d, 4) if vf else None,
            "variacion_14d": round(vf.change_14d, 4) if vf else None,
            "variacion_valor_7d_eur": round(vf.value * vf.change_7d) if vf else None,
            "variacion_valor_14d_eur": round(vf.value * vf.change_14d) if vf else None,
            "venta_al_juego": "valor de mercado ±5 %",
            "clausula": clause[0] if clause else None,
        }

    def estado_rivales(self) -> dict[str, Any]:
        s = self.snap
        est = estimate_balances(self.conn)
        own = next((b for b in est if b.actual is not None), None)
        standings = {
            int(r[0]): (r[1], r[2])
            for r in self.conn.execute(
                "select manager_id, season_rank, season_points from manager_snapshot"
                " where snapshot_date = %s",
                (s.snapshot_date,),
            )
        }
        return {
            **self._base(),
            "aviso": "saldos rivales estimados (ocultos en la liga)",
            "error_estimacion_con_mi_saldo": own.error if own else None,
            "rivales": sorted(
                [
                    {"nombre": b.name, "puesto": standings.get(b.manager_id, (None, None))[0],
                     "puntos": standings.get(b.manager_id, (None, None))[1],
                     "valor_equipo": b.team_value, "saldo_estimado": b.estimated,
                     "gasto_posible_con_deuda": b.spendable()}
                    for b in est if b.manager_id != s.me_id and (b.team_value or 0) > 0
                ],
                key=lambda r: r["puesto"] or 99,
            ),
        }  # fmt: skip

    def mercado_hoy(self) -> dict[str, Any]:
        s = self.snap
        rows = self.conn.execute(
            "select player_id from market_snapshot"
            " where snapshot_date = (select max(snapshot_date) from market_snapshot)"
        ).fetchall()
        out = []
        for (pid,) in rows:
            if int(pid) in s.squad:
                continue
            try:
                cand, _ = self._candidate(int(pid), None)
            except ToolError:
                continue
            v = mk.value(s.squad_options(), cand, balance=s.balance)
            out.append(
                {**_name(s, int(pid)), "precio": cand.price, "valor": cand.value,
                 "puntos_extra_5_jornadas": v.horizon_points, "puntuacion": v.score,
                 "variacion_14d": round(cand.change_14d, 4), "cabe_con_saldo": v.affordable_now}
            )  # fmt: skip
        out.sort(key=lambda r: -r["puntuacion"])
        return {**self._base(), "saldo": s.balance, "mejores": out[:8]}

    def registrar_ajuste(
        self,
        player_id: int,
        estado: str,
        p_jugar: float | None,
        hasta: str,
        fuente: str,
        fecha_fuente: str,
        cita: str,
    ) -> dict[str, Any]:
        if player_id not in self.snap.names:
            raise ToolError(f"no existe el jugador {player_id}")
        try:
            until, src_date = date.fromisoformat(hasta), date.fromisoformat(fecha_fuente)
        except ValueError:
            raise ToolError("fechas en formato AAAA-MM-DD") from None
        if p_jugar is not None and not 0 <= p_jugar <= 1:
            raise ToolError("p_jugar va de 0 a 1")
        if not cita.strip() or not fuente.strip():
            raise ToolError("hace falta la fuente y la frase de la noticia")
        with self.conn.transaction():
            row = self.conn.execute(
                "insert into availability_overrides (player_id, status, p_play, valid_until,"
                " source, source_date, quote) values (%s, %s, %s, %s, %s, %s, %s) returning id",
                (player_id, estado, p_jugar, until, fuente, src_date, cita),
            ).fetchone()
        # Las predicciones guardadas no lo recogen: la próxima ejecución, sí.
        return {"registrado": True, "id": row[0] if row else None,
                "jugador": _name(self.snap, player_id), "estado": estado, "hasta": hasta,
                "nota": "se aplicará en la próxima ejecución de predicciones"}  # fmt: skip

    def ajustes_vigentes(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "select id, player_id, status, p_play, valid_until, source, source_date, quote"
            " from availability_overrides where active and valid_until >= current_date"
            " order by source_date desc"
        ).fetchall()
        return {
            "ajustes": [
                {"id": i, "jugador": _name(self.snap, p), "estado": st, "p_jugar": pp,
                 "hasta": str(vu), "fuente": src, "fecha": str(sd), "cita": q}
                for i, p, st, pp, vu, src, sd, q in rows
            ]
        }  # fmt: skip


def options_from(snap: Snapshot) -> list[Option]:
    return snap.squad_options()
