"""Entradas del modelo v0: historial por jugador, contexto del partido y priors de la liga.

Todo se carga de la BD en cada ejecución (son pocos miles de filas). Las
funciones del modelo (minutes.py, points.py) son puras y reciben estos objetos.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import psycopg

from mister_assistant.scoring.mixed import extras

Conn = psycopg.Connection[tuple[Any, ...]]
RESULTS = ("V", "E", "D")
HISTORY_MATCHES = 5


@dataclass(frozen=True)
class Appearance:
    """Un partido ya jugado del equipo del jugador (haya jugado él o no)."""

    gameweek_number: int
    played: bool
    started: bool
    minutes: int
    points_mix: int | None
    base_mix: float | None  # Mixta sin el bonus de eventos
    result: str  # V/E/D para su equipo
    # Notas por fuente (AS, Marca, MD, SofaScore); None = S.C. o no jugó
    ratings: tuple[int | None, int | None, int | None, float | None] = (None, None, None, None)


@dataclass(frozen=True)
class FixtureContext:
    fixture_id: int
    home_team_id: int
    away_team_id: int
    # Probabilidades sin margen (cuotas) desde el punto de vista del local; None sin cuotas
    p_home: float | None = None
    p_draw: float | None = None
    p_away: float | None = None
    total_goals: float | None = None  # goles esperados del partido (de más/menos 2,5)

    def result_probs(self, team_id: int) -> dict[str, float] | None:
        if self.p_home is None or self.p_draw is None or self.p_away is None:
            return None
        win, loss = (
            (self.p_home, self.p_away)
            if team_id == self.home_team_id
            else (self.p_away, self.p_home)
        )
        return {"V": win, "E": self.p_draw, "D": loss}


@dataclass
class PlayerFeatures:
    player_id: int
    name: str
    position: int
    team_id: int
    fixture: FixtureContext | None
    history: list[Appearance] = field(default_factory=list)
    xg: float = 0.0
    xg_minutes: int = 0
    ff_prob: float | None = None
    ff_role: str | None = None
    ff_injury: int | None = None
    mister_xi: bool | None = None


@dataclass(frozen=True)
class LeaguePriors:
    base_by_result: dict[
        int, dict[str, float]
    ]  # titulares: Mixta sin bonus por posición y resultado
    start_sd: dict[int, float]
    sub_mean: dict[int, float]
    sub_sd: dict[int, float]
    xg90: dict[int, float]
    start_minutes: dict[int, float]
    sub_minutes: float
    result_dist: dict[str, float]  # reparto V/E/D sin cuotas
    team_goals: float  # goles por equipo y partido
    card_ev: float  # puntos esperados por tarjetas (doble amarilla / roja) por partido


DEFAULT_PRIORS = LeaguePriors(
    base_by_result={p: {"V": 4.5, "E": 3.5, "D": 2.5} for p in (1, 2, 3, 4)},
    start_sd={1: 2.7, 2: 2.8, 3: 3.0, 4: 3.5},
    sub_mean={1: 1.0, 2: 2.3, 3: 2.7, 4: 3.1},
    sub_sd={1: 2.0, 2: 1.8, 3: 2.3, 4: 2.8},
    xg90={1: 0.0, 2: 0.04, 3: 0.11, 4: 0.45},
    start_minutes={1: 89.0, 2: 83.0, 3: 77.0, 4: 77.0},
    sub_minutes=22.0,
    result_dist={"V": 0.37, "E": 0.26, "D": 0.37},
    team_goals=1.3,
    card_ev=-0.04,
)


def _result(team_id: int, home: int, gh: int | None, ga: int | None) -> str | None:
    if gh is None or ga is None:
        return None
    mine, theirs = (gh, ga) if team_id == home else (ga, gh)
    return "V" if mine > theirs else "E" if mine == theirs else "D"


# Sin límite: todas las jornadas. En el backtest, solo las anteriores a la objetivo.
NO_LIMIT = 10_000


def load_priors(conn: Conn, before_number: int | None = None) -> LeaguePriors:
    """Parámetros de la liga. Con `before_number`, solo con jornadas anteriores."""
    limit = before_number or NO_LIMIT
    rows = conn.execute(
        "select p.position, p.team_id, p.minutes, p.points_mix, p.sub_in_minute, p.goals,"
        " p.penalty_goals, p.double_yellow, p.red_cards, f.home_team_id, f.goals_home,"
        " f.goals_away"
        " from player_gameweek p join fixtures f on f.id = p.match_id"
        " join gameweeks g on g.id = p.gameweek_id"
        " where p.points_mix is not null and p.position between 1 and 4 and g.number < %s",
        (limit,),
    ).fetchall()
    if len(rows) < 200:
        return DEFAULT_PRIORS
    base: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    starts: dict[int, list[float]] = defaultdict(list)
    start_min: dict[int, list[int]] = defaultdict(list)
    subs: dict[int, list[float]] = defaultdict(list)
    sub_min: list[int] = []
    results: dict[str, int] = defaultdict(int)
    cards: list[float] = []
    for pos, team, minutes, mix, sub_in, g, pg, dy, red, home, gh, ga in rows:
        res = _result(team, home, gh, ga)
        bonus = extras(pos, g, pg, dy, red)
        cards.append(-3.0 * dy - 6.0 * red)
        if sub_in is None:
            if res is None:
                continue
            b = float(mix) - bonus
            base[pos][res].append(b)
            starts[pos].append(b)
            start_min[pos].append(int(minutes or 0))
            results[res] += 1
        else:
            subs[pos].append(float(mix))
            sub_min.append(int(minutes or 0))
    xg_rows = conn.execute(
        "select pg.position, sum(ms.xg), sum(ms.minutes) from match_stats ms"
        " join player_gameweek pg on pg.player_id = ms.player_id and pg.match_id = ms.fixture_id"
        " join gameweeks g on g.id = pg.gameweek_id where g.number < %s group by 1",
        (limit,),
    ).fetchall()
    goals_row = conn.execute(
        "select avg(f.goals_home + f.goals_away) / 2.0 from fixtures f"
        " join gameweeks g on g.id = f.gameweek_id where f.status = 'played' and g.number < %s",
        (limit,),
    ).fetchone()
    d = DEFAULT_PRIORS

    def mean(xs: list[float] | list[int], default: float) -> float:
        return float(sum(xs) / len(xs)) if len(xs) >= 10 else default

    def sd(xs: list[float], default: float) -> float:
        if len(xs) < 10:
            return default
        m = sum(xs) / len(xs)
        return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))

    total_results = sum(results.values()) or 1
    return LeaguePriors(
        base_by_result={
            p: {r: mean(base[p][r], d.base_by_result[p][r]) for r in RESULTS} for p in (1, 2, 3, 4)
        },
        start_sd={p: sd(starts[p], d.start_sd[p]) for p in (1, 2, 3, 4)},
        sub_mean={p: mean(subs[p], d.sub_mean[p]) for p in (1, 2, 3, 4)},
        sub_sd={p: sd(subs[p], d.sub_sd[p]) for p in (1, 2, 3, 4)},
        xg90={
            int(p): (float(x) * 90 / float(m) if m else d.xg90[int(p)])
            for p, x, m in xg_rows
            if p in (1, 2, 3, 4)
        }
        | {p: d.xg90[p] for p in (1, 2, 3, 4) if p not in {r[0] for r in xg_rows}},
        start_minutes={p: mean(start_min[p], d.start_minutes[p]) for p in (1, 2, 3, 4)},
        sub_minutes=mean(sub_min, d.sub_minutes),
        result_dist={r: results[r] / total_results for r in RESULTS},
        team_goals=float(goals_row[0]) if goals_row and goals_row[0] is not None else d.team_goals,
        card_ev=mean(cards, d.card_ev),
    )


def poisson_total_from_over(p_over_25: float) -> float:
    """λ tal que P(goles ≥ 3) = p (Poisson), por bisección."""
    p_over_25 = min(max(p_over_25, 0.05), 0.95)
    lo, hi = 0.1, 8.0
    for _ in range(60):
        lam = (lo + hi) / 2
        p_le2 = math.exp(-lam) * (1 + lam + lam * lam / 2)
        if 1 - p_le2 < p_over_25:
            lo = lam
        else:
            hi = lam
    return (lo + hi) / 2


def load_fixture_contexts(
    conn: Conn, gameweek_id: int, *, backtest: bool = False
) -> dict[int, FixtureContext]:
    """Partidos de la jornada con sus cuotas. En el backtest: todos, y sin cuotas."""
    fixtures = conn.execute(
        "select id, home_team_id, away_team_id from fixtures"
        " where gameweek_id = %s and (%s or status is distinct from 'played')",
        (gameweek_id, backtest),
    ).fetchall()
    out: dict[int, FixtureContext] = {}
    for fid, home, away in fixtures:
        if backtest:
            out[int(fid)] = FixtureContext(int(fid), int(home), int(away))
            continue
        odds = conn.execute(
            "select market, outcome, point, prob_fair from odds where fixture_id = %s"
            " and captured_at = (select max(captured_at) from odds where fixture_id = %s)",
            (fid, fid),
        ).fetchall()
        h2h = {o: float(p) for m, o, _, p in odds if m == "h2h"}
        over = next(
            (
                float(p)
                for m, o, pt, p in odds
                if m == "totals" and o == "over" and float(pt) == 2.5
            ),
            None,
        )
        out[int(fid)] = FixtureContext(
            fixture_id=int(fid),
            home_team_id=int(home),
            away_team_id=int(away),
            p_home=h2h.get("home"),
            p_draw=h2h.get("draw"),
            p_away=h2h.get("away"),
            total_goals=poisson_total_from_over(over) if over is not None else None,
        )
    return out


def load_player_features(
    conn: Conn, gameweek_id: int, *, backtest: bool = False
) -> list[PlayerFeatures]:
    """Jugadores de los equipos que juegan la jornada, con su historial y alineaciones.

    `backtest=True` reconstruye lo que se sabía antes de una jornada ya jugada:
    historial y xG solo de jornadas anteriores, y sin Fútbol Fantasy, once de
    Mister ni cuotas (no se capturaban entonces).
    """
    target = conn.execute("select number from gameweeks where id = %s", (gameweek_id,)).fetchone()
    limit = int(target[0]) if backtest and target else NO_LIMIT
    contexts = load_fixture_contexts(conn, gameweek_id, backtest=backtest)
    team_fixture: dict[int, FixtureContext] = {}
    for ctx in contexts.values():
        team_fixture[ctx.home_team_id] = ctx
        team_fixture[ctx.away_team_id] = ctx

    team_matches: dict[int, list[tuple[int, int, int, int | None, int | None]]] = defaultdict(list)
    for fid, number, home, away, gh, ga in conn.execute(
        "select f.id, g.number, f.home_team_id, f.away_team_id, f.goals_home, f.goals_away"
        " from fixtures f join gameweeks g on g.id = f.gameweek_id"
        " where f.status = 'played' and g.number < %s order by g.number desc, f.kickoff_at desc",
        (limit,),
    ):
        for team in (home, away):
            if len(team_matches[team]) < HISTORY_MATCHES:
                team_matches[team].append((int(fid), int(number), int(home), gh, ga))

    rows_by_player: dict[int, dict[int, tuple[Any, ...]]] = defaultdict(dict)
    for r in conn.execute(
        "select player_id, match_id, minutes, points_mix, sub_in_minute, goals, penalty_goals,"
        " double_yellow, red_cards, rating_as, rating_marca, rating_md, rating_sofascore"
        " from player_gameweek where match_id is not null"
    ):
        rows_by_player[int(r[0])][int(r[1])] = r

    xg = {
        int(r[0]): (float(r[1]), int(r[2] or 0))
        for r in conn.execute(
            "select ms.player_id, sum(ms.xg), sum(ms.minutes) from match_stats ms"
            " join gameweeks g on g.id = ms.gameweek_id where g.number < %s group by 1",
            (limit,),
        )
    }
    ff = {
        int(r[0]): (float(r[1]) if r[1] is not None else None, r[2], r[3])
        for r in conn.execute(
            "select distinct on (x.player_id) x.player_id, l.probability, l.role, l.injury_code"
            " from lineup_forecast l join player_xref x"
            "  on x.source = l.source and x.external_id = l.external_id"
            " where l.source = 'futbolfantasy' and l.gameweek_id = %s and not %s"
            " order by x.player_id, l.captured_at desc",
            (gameweek_id, backtest),
        )
    }
    mister_capture = conn.execute(
        "select max(captured_at) from lineup_forecast where source = 'mister' and gameweek_id = %s",
        (gameweek_id,),
    ).fetchone()
    mister_xi: set[int] | None = None
    if mister_capture and mister_capture[0] is not None and not backtest:
        mister_xi = {
            int(r[0])
            for r in conn.execute(
                "select external_id from lineup_forecast where source = 'mister'"
                " and gameweek_id = %s and captured_at = %s",
                (gameweek_id, mister_capture[0]),
            )
        }

    out: list[PlayerFeatures] = []
    for pid, name, pos, team in conn.execute(
        "select mister_player_id, name, position, team_id from players"
        " where position between 1 and 4 and team_id = any(%s)",
        (list(team_fixture),),
    ):
        history = []
        my_rows = rows_by_player.get(int(pid), {})
        for fid, number, home, gh, ga in team_matches.get(int(team), []):
            res = _result(int(team), home, gh, ga) or "E"
            row = my_rows.get(fid)
            if row is None or not row[2]:
                history.append(Appearance(number, False, False, 0, None, None, res))
                continue
            _, _, minutes, mix, sub_in, g, pg, dy, red, r_as, r_marca, r_md, r_ss = row
            base = float(mix) - extras(int(pos), g, pg, dy, red) if mix is not None else None
            ratings = (r_as, r_marca, r_md, float(r_ss) if r_ss is not None else None)
            history.append(
                Appearance(number, True, sub_in is None, int(minutes), mix, base, res, ratings)
            )
        f = ff.get(int(pid))
        x = xg.get(int(pid), (0.0, 0))
        out.append(
            PlayerFeatures(
                player_id=int(pid),
                name=str(name),
                position=int(pos),
                team_id=int(team),
                fixture=team_fixture.get(int(team)),
                history=history,
                xg=x[0],
                xg_minutes=x[1],
                ff_prob=f[0] if f else None,
                ff_role=f[1] if f else None,
                ff_injury=f[2] if f else None,
                mister_xi=(int(pid) in mister_xi) if mister_xi is not None else None,
            )
        )
    return out
