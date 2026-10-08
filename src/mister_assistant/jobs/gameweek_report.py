"""Job `gameweek-report`: predicciones, once recomendado e informe por Telegram.

En modo automático solo actúa en dos ventanas antes del primer partido de la
próxima jornada (`vispera`, unas 24 h antes, y `previa`, unas 3 h antes) y una vez
por ventana; fuera de ellas no hace ninguna petición. Las predicciones de todos
los jugadores de la jornada se guardan siempre antes del primer partido.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from mister_assistant.decide.lineup import FORMATIONS, Lineup, Option, evaluate, recommend
from mister_assistant.models.features import load_player_features, load_priors
from mister_assistant.models.points import MODEL_VERSION, PointsForecast, forecast_points
from mister_assistant.sources.mister import MisterClient
from mister_assistant.store import repo

log = logging.getLogger(__name__)

MADRID = ZoneInfo("Europe/Madrid")
Conn = psycopg.Connection[tuple[Any, ...]]

# (ventana, desde, hasta) en horas antes del primer partido
SLOTS: tuple[tuple[str, float, float], ...] = (("vispera", 20.0, 28.0), ("previa", 2.0, 4.0))
POSITION_LABEL = {1: "POR", 2: "DEF", 3: "CEN", 4: "DEL"}


@dataclass(frozen=True)
class NextGameweek:
    gameweek_id: int
    number: int
    first_kickoff: datetime


@dataclass
class ReportResult:
    gameweek: NextGameweek | None
    slot: str | None
    run_id: int | None = None
    message: str | None = None
    lineup: Lineup | None = None
    skipped_reason: str | None = None


def next_gameweek(conn: Conn, now: datetime) -> NextGameweek | None:
    """Próxima jornada sin empezar (las `ongoing` con un aplazado no cuentan)."""
    row = conn.execute(
        # El primer partido se calcula sobre todos los de la jornada: si ya empezó
        # (aunque la BD aún diga «unstarted»), no es la próxima.
        "select g.id, g.number, min(f.kickoff_at) from gameweeks g"
        " join fixtures f on f.gameweek_id = g.id"
        " where g.status = 'unstarted'"
        " group by g.id, g.number having min(f.kickoff_at) > %s"
        " order by min(f.kickoff_at) limit 1",
        (now,),
    ).fetchone()
    if row is None:
        return None
    return NextGameweek(int(row[0]), int(row[1]), row[2])


def due_slot(conn: Conn, gw: NextGameweek, now: datetime) -> str | None:
    hours = (gw.first_kickoff - now).total_seconds() / 3600
    for slot, lo, hi in SLOTS:
        if lo <= hours <= hi:
            sent = conn.execute(
                "select 1 from report_log where gameweek_id = %s and slot = %s",
                (gw.gameweek_id, slot),
            ).fetchone()
            return None if sent else slot
    return None


def my_manager(conn: Conn) -> tuple[int, str | None]:
    row = conn.execute(
        "select mister_manager_id, slug from managers where is_me limit 1"
    ).fetchone()
    if row is None:
        raise ValueError("no hay mánager propio en la BD: ejecuta antes snapshot-daily")
    return int(row[0]), row[1]


def current_lineup_ids(gameweek_data: Any) -> list[int]:
    positions = (gameweek_data.get("lineup") or {}).get("positions") or {}
    ids: list[int] = []
    for slots in positions.values():
        for p in slots.values() if isinstance(slots, dict) else slots:
            if isinstance(p, dict) and p.get("id"):
                ids.append(int(p["id"]))
    return ids


def build_report(
    conn: Conn,
    mister: MisterClient,
    gw: NextGameweek,
    *,
    slot: str,
    now: datetime,
) -> ReportResult:
    me_id, me_slug = my_manager(conn)
    gw_resp = mister.gameweek(gw.gameweek_id)
    user_resp = mister.user(me_id, me_slug or "")
    run_date = now.astimezone(MADRID).date()
    with conn.transaction():
        repo.save_raw(conn, gw_resp, run_date)
        repo.save_raw(conn, user_resp, run_date)
    squad_ids = [int(p["id"]) for p in user_resp.data.get("team_now") or []]
    current_ids = current_lineup_ids(gw_resp.data)

    priors = load_priors(conn)
    features = load_player_features(conn, gw.gameweek_id)
    forecasts = {f.player_id: (f, forecast_points(f, priors)) for f in features}

    names = {
        int(r[0]): (str(r[1]), r[2], r[3], r[4])
        for r in conn.execute(
            "select p.mister_player_id, coalesce(p.short_name, p.name), p.position, t.name,"
            " p.team_id from players p left join teams t on t.id = p.team_id"
            " where p.mister_player_id = any(%s)",
            (squad_ids,),
        )
    }
    squad: list[Option] = []
    for pid in squad_ids:
        name, pos, _, _ = names.get(pid, (str(pid), None, None, None))
        fc = forecasts.get(pid)
        if fc is None:
            # Su equipo no juega esta jornada (o no lo conocemos): 0 puntos.
            if pos in (1, 2, 3, 4):
                squad.append(Option(pid, name, int(pos), 0.0, 0.0))
            continue
        f, pf = fc
        squad.append(
            Option(pid, name, f.position, pf.exp_points, pf.minutes.p_play, pf.p20, pf.p80)
        )
    lineup = recommend(squad, FORMATIONS)
    current_expected = evaluate(squad, current_ids) if current_ids else None

    with conn.transaction():
        run_id = _save(conn, gw, slot, now, forecasts, me_id, lineup, current_ids, current_expected)
    message = format_report(gw, lineup, squad, current_ids, current_expected, names, now)
    return ReportResult(gw, slot, run_id, message, lineup)


def _save(
    conn: Conn,
    gw: NextGameweek,
    slot: str,
    now: datetime,
    forecasts: dict[int, tuple[Any, PointsForecast]],
    me_id: int,
    lineup: Lineup,
    current_ids: list[int],
    current_expected: float | None,
) -> int:
    row = conn.execute(
        "insert into prediction_runs (gameweek_id, model_version, created_at, first_kickoff_at,"
        "  before_kickoff, trigger, inputs)"
        " values (%s, %s, %s, %s, %s, %s, %s) returning id",
        (
            gw.gameweek_id, MODEL_VERSION, now, gw.first_kickoff, now < gw.first_kickoff, slot,
            Jsonb({"players": len(forecasts)}),
        ),
    ).fetchone()  # fmt: skip
    assert row is not None
    run_id = int(row[0])
    with conn.cursor() as cur:
        cur.executemany(
            "insert into predictions (run_id, player_id, gameweek_id, fixture_id, team_id,"
            "  position, p_start, p_sub, p_play, exp_minutes, exp_points, p20, p80, components)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    run_id, f.player_id, gw.gameweek_id,
                    f.fixture.fixture_id if f.fixture else None, f.team_id, f.position,
                    round(pf.minutes.p_start, 3), round(pf.minutes.p_sub, 3),
                    round(pf.minutes.p_play, 3), round(pf.minutes.exp_minutes, 1),
                    pf.exp_points, pf.p20, pf.p80,
                    Jsonb({**pf.components, "fuente_minutos": pf.minutes.source}),
                )
                for f, pf in forecasts.values()
            ],
        )  # fmt: skip
    conn.execute(
        "insert into lineup_recommendations (run_id, manager_id, gameweek_id, formation,"
        "  player_ids, expected_points, current_player_ids, current_expected, fragile)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            run_id, me_id, gw.gameweek_id, lineup.formation, lineup.player_ids,
            round(lineup.expected_points, 2), current_ids or None,
            round(current_expected, 2) if current_expected is not None else None,
            Jsonb([fr.__dict__ for fr in lineup.fragile]),
        ),
    )  # fmt: skip
    return run_id


def log_sent(conn: Conn, gw: NextGameweek, slot: str, run_id: int | None, delivered: bool) -> None:
    conn.execute(
        "insert into report_log (gameweek_id, slot, run_id, delivered) values (%s, %s, %s, %s)"
        " on conflict (gameweek_id, slot) do update set run_id = excluded.run_id,"
        " delivered = excluded.delivered, sent_at = now()",
        (gw.gameweek_id, slot, run_id, delivered),
    )


# -- mensaje ---------------------------------------------------------------------

_DAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


def _num(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def _team_short(name: str | None) -> str:
    if not name:
        return "?"
    words = [w for w in name.replace("Real ", "").split() if w[0].isupper()]
    return (words[0][:3] if words else name[:3]).upper()


def format_report(
    gw: NextGameweek,
    lineup: Lineup,
    squad: list[Option],
    current_ids: list[int],
    current_expected: float | None,
    names: dict[int, tuple[str, Any, Any, Any]],
    now: datetime,
) -> str:
    kick = gw.first_kickoff.astimezone(MADRID)
    by_id = {o.player_id: o for o in squad}

    def label(o: Option) -> str:
        team = _team_short(names.get(o.player_id, ("", None, None, None))[2])
        rng = f"[{_num(o.p20)} a {_num(o.p80)}]"
        return f"{o.name} ({team}) {_num(o.exp_points)} {rng} · {o.p_play:.0%}"

    lines = [
        f"⚽ J{gw.number} · once recomendado {lineup.formation}"
        f" · {_num(lineup.expected_points)} pts",
        f"Primer partido: {_DAYS[kick.weekday()]} {kick:%d/%m %H:%M}",
        "",
    ]
    for pos in (1, 2, 3, 4):
        for o in sorted(
            (p for p in lineup.players if p.position == pos), key=lambda o: -o.exp_points
        ):
            lines.append(f"{POSITION_LABEL[pos]}  {label(o)}")
    if lineup.empty_slots:
        lines.append(f"❗ {lineup.empty_slots} hueco(s) sin cubrir: -4 cada uno")

    if lineup.fragile:
        lines += ["", "⚠️ Dudas"]
        for fr in lineup.fragile:
            o = by_id[fr.player_id]
            rep = by_id.get(fr.replacement_id) if fr.replacement_id else None
            if rep is None:
                alt = "sin recambio"
            elif abs(fr.cost) < 0.05:
                alt = f"si no, {rep.name} (sin coste)"
            else:
                alt = f"si no, {rep.name} ({_num(-fr.cost)} pts)"
            lines.append(f"- {o.name} juega al {fr.p_play:.0%}; {alt}")

    if current_ids:
        out = [i for i in current_ids if i not in lineup.player_ids]
        inn = [i for i in lineup.player_ids if i not in current_ids]
        assert current_expected is not None
        gain = lineup.expected_points - current_expected
        lines += ["", f"🔁 Tu once actual: {_num(current_expected)} pts"]
        if not out and not inn:
            lines.append("Ya es el recomendado. No toques nada.")
        else:
            lines.append(f"Cambios (+{_num(gain)} pts):")
            for i in out:
                gone = by_id.get(i)
                text = f"{gone.name} ({_num(gone.exp_points)})" if gone else f"{i} (ya no está)"
                lines.append(f"- Fuera {text}")
            for i in inn:
                new = by_id[i]
                lines.append(f"- Dentro {new.name} ({_num(new.exp_points)})")
    else:
        lines += ["", "🔁 No tienes once puesto para esta jornada en la app."]

    bench = sorted(
        (o for o in squad if o.player_id not in lineup.player_ids), key=lambda o: -o.exp_points
    )[:4]
    if bench:
        lines += ["", "Banquillo: " + ", ".join(f"{o.name} {_num(o.exp_points)}" for o in bench)]
    lines += ["", f"Modelo {MODEL_VERSION} · {now.astimezone(MADRID):%d/%m %H:%M}"]
    return "\n".join(lines)


def manual_slot(now: datetime) -> str:
    return f"manual-{now.astimezone(UTC):%Y%m%dT%H%M}"


def hours_until(gw: NextGameweek, now: datetime) -> float:
    return (gw.first_kickoff - now) / timedelta(hours=1)
