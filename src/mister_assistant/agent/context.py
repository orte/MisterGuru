"""Datos que consultan las herramientas del agente (solo lectura sobre la BD).

Toda cifra de puntos sale de una ejecución guardada en `predictions`: si no hay
una reciente de la próxima jornada, se crea (disparador `agente`) con los datos
de la BD, sin peticiones a Mister. Así cada respuesta es trazable a un run_id.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from mister_assistant.decide.lineup import Option
from mister_assistant.models.features import load_player_features, load_priors
from mister_assistant.models.points import MODEL_VERSION, forecast_points
from mister_assistant.models.value import ValueForecast, forecast_all

Conn = psycopg.Connection[tuple[Any, ...]]
MAX_RUN_AGE = timedelta(hours=12)
VALUE_CACHE_SECONDS = 3600
POSITION = {1: "POR", 2: "DEF", 3: "CEN", 4: "DEL"}


@dataclass(frozen=True)
class PlayerPrediction:
    player_id: int
    exp_points: float
    p_play: float
    p_start: float
    p20: float
    p80: float
    fixture_id: int | None
    components: dict[str, Any]


@dataclass
class Snapshot:
    run_id: int
    gameweek_id: int
    gameweek_number: int
    first_kickoff: datetime
    predictions: dict[int, PlayerPrediction]
    me_id: int
    balance: int
    max_bid: int
    team_value: int | None
    squad: list[int]
    snapshot_date: Any
    names: dict[int, tuple[str, int | None, str | None]] = field(default_factory=dict)

    def option(self, pid: int) -> Option | None:
        name, pos, _ = self.names.get(pid, (str(pid), None, None))
        if pos not in (1, 2, 3, 4):
            return None
        pr = self.predictions.get(pid)
        return Option(pid, name, int(pos), pr.exp_points if pr else 0.0, pr.p_play if pr else 0.0)

    def squad_options(self) -> list[Option]:
        return [o for p in self.squad if (o := self.option(p)) is not None]


def next_gameweek(conn: Conn) -> tuple[int, int, datetime] | None:
    row = conn.execute(
        "select g.id, g.number, min(f.kickoff_at) from gameweeks g"
        " join fixtures f on f.gameweek_id = g.id where g.status = 'unstarted'"
        " group by g.id, g.number having min(f.kickoff_at) > now()"
        " order by min(f.kickoff_at) limit 1"
    ).fetchone()
    return (int(row[0]), int(row[1]), row[2]) if row else None


def ensure_run(
    conn: Conn, gameweek_id: int, first_kickoff: datetime, trigger: str = "agente"
) -> int:
    """Última ejecución reciente de la jornada; si no hay, se crea y se guarda."""
    row = conn.execute(
        "select id from prediction_runs where gameweek_id = %s and created_at > %s"
        " order by created_at desc limit 1",
        (gameweek_id, datetime.now(UTC) - MAX_RUN_AGE),
    ).fetchone()
    if row is not None:
        return int(row[0])
    priors = load_priors(conn)
    features = load_player_features(conn, gameweek_id)
    now = datetime.now(UTC)
    with conn.transaction():
        run = conn.execute(
            "insert into prediction_runs (gameweek_id, model_version, created_at,"
            " first_kickoff_at, before_kickoff, trigger, inputs)"
            " values (%s, %s, %s, %s, %s, %s, %s) returning id",
            (gameweek_id, MODEL_VERSION, now, first_kickoff, now < first_kickoff, trigger,
             Jsonb({"players": len(features)})),
        ).fetchone()  # fmt: skip
        assert run is not None
        run_id = int(run[0])
        rows = []
        for f in features:
            pf = forecast_points(f, priors)
            rows.append(
                (run_id, f.player_id, gameweek_id, f.fixture.fixture_id if f.fixture else None,
                 f.team_id, f.position, round(pf.minutes.p_start, 3), round(pf.minutes.p_sub, 3),
                 round(pf.minutes.p_play, 3), round(pf.minutes.exp_minutes, 1), pf.exp_points,
                 pf.p20, pf.p80, Jsonb({**pf.components, "fuente_minutos": pf.minutes.source}))
            )  # fmt: skip
        with conn.cursor() as cur:
            cur.executemany(
                "insert into predictions (run_id, player_id, gameweek_id, fixture_id, team_id,"
                " position, p_start, p_sub, p_play, exp_minutes, exp_points, p20, p80, components)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )
    return run_id


def load_snapshot(conn: Conn, trigger: str = "agente") -> Snapshot:
    gw = next_gameweek(conn)
    if gw is None:
        raise ValueError("no hay ninguna jornada próxima en la BD")
    gw_id, number, kickoff = gw
    run_id = ensure_run(conn, gw_id, kickoff, trigger)
    preds = {
        int(r[0]): PlayerPrediction(
            int(r[0]),
            float(r[1]),
            float(r[2]),
            float(r[3]),
            float(r[4]),
            float(r[5]),
            r[6],
            dict(r[7] or {}),
        )
        for r in conn.execute(
            "select player_id, exp_points, p_play, p_start, p20, p80, fixture_id, components"
            " from predictions where run_id = %s",
            (run_id,),
        )
    }
    me = conn.execute(
        "select m.mister_manager_id, s.balance, s.max_bid, s.team_value, s.snapshot_date"
        " from managers m join manager_snapshot s on s.manager_id = m.mister_manager_id"
        " where m.is_me order by s.snapshot_date desc limit 1"
    ).fetchone()
    if me is None:
        raise ValueError("no hay snapshot del mánager propio")
    squad = [
        int(r[0])
        for r in conn.execute(
            "select player_id from squad_snapshot where manager_id = %s and snapshot_date = %s",
            (me[0], me[4]),
        )
    ]
    names = {
        int(r[0]): (str(r[1]), r[2], r[3])
        for r in conn.execute(
            "select p.mister_player_id, coalesce(p.short_name, p.name), p.position, t.name"
            " from players p left join teams t on t.id = p.team_id"
        )
    }
    return Snapshot(
        run_id, gw_id, number, kickoff, preds, int(me[0]), int(me[1] or 0), int(me[2] or 0),
        int(me[3]) if me[3] is not None else None, squad, me[4], names,
    )  # fmt: skip


_value_cache: tuple[float, dict[int, ValueForecast]] | None = None


def value_forecasts(conn: Conn) -> dict[int, ValueForecast]:
    """Previsión de valor (cara de calcular: se reutiliza durante una hora)."""
    global _value_cache
    if _value_cache is None or time.monotonic() - _value_cache[0] > VALUE_CACHE_SECONDS:
        _, values = forecast_all(conn)
        _value_cache = (time.monotonic(), values)
    return _value_cache[1]
