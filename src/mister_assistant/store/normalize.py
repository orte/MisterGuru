"""De respuestas de Mister a filas de las tablas (funciones puras, sin red ni BD)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from mister_assistant.sources.mister_html import MarketRow, StandingRow

# Las fechas «YYYY-MM-DD HH:MM:SS» de Mister van en hora de Madrid.
MADRID = ZoneInfo("Europe/Madrid")


class NormalizeError(ValueError):
    """Respuesta con una forma que la normalización no reconoce."""


@dataclass(frozen=True)
class Row:
    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlayerRow(Row):
    mister_player_id: int
    name: str
    short_name: str | None
    slug: str | None
    position: int | None
    team_id: int | None


@dataclass(frozen=True)
class ManagerRow(Row):
    mister_manager_id: int
    community_id: int
    name: str
    slug: str | None
    is_me: bool


@dataclass(frozen=True)
class GameweekRow(Row):
    id: int
    number: int
    season: str | None
    type: str | None
    status: str | None
    first_match_at: datetime | None
    last_match_at: datetime | None


@dataclass(frozen=True)
class FixtureRow(Row):
    id: int
    gameweek_id: int
    home_team_id: int
    away_team_id: int
    kickoff_at: datetime | None
    status: str | None
    goals_home: int | None
    goals_away: int | None
    sofascore_id: int | None


@dataclass(frozen=True)
class ManagerSnapshotRow(Row):
    manager_id: int
    snapshot_date: date
    season_rank: int | None
    season_points: int | None
    team_value: int | None
    team_value_prev: int | None
    squad_size: int | None
    balance: int | None = None
    balance_future: int | None = None
    max_bid: int | None = None


@dataclass(frozen=True)
class SquadSnapshotRow(Row):
    player_id: int
    snapshot_date: date
    manager_id: int
    market_value: int | None
    clause_value: int | None
    clause_floor: int | None
    clause_multiplier: float | None
    clause_shield: int | None
    transfer_origin: str | None
    on_sale_price: int | None
    in_lineup: bool


@dataclass(frozen=True)
class MarketSnapshotRow(Row):
    player_id: int
    snapshot_date: date
    market_value: int | None
    value_trend: int
    points: int | None
    avg_points: float | None
    on_sale: bool
    sale_price: int | None
    seller_manager_id: int | None
    sale_ends_at: datetime | None


@dataclass(frozen=True)
class LeagueEventRow(Row):
    event_key: str
    community_id: int
    feed_card_id: int
    category: str
    event_type: str | None
    occurred_at: datetime
    player_id: int | None
    from_manager_id: int | None
    to_manager_id: int | None
    price: int | None
    payload: str  # JSON


@dataclass(frozen=True)
class PlayedPlayer:
    """Jugador que disputó un partido de una jornada cerrada (de /ajax/sw/gameweek)."""

    player: PlayerRow
    match_id: int
    gameweek_id: int
    points: int | None
    events: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PlayerGameweekRow(Row):
    player_id: int
    gameweek_id: int
    match_id: int | None
    team_id: int | None
    position: int | None
    minutes: int | None
    rating_as: int | None
    rating_marca: int | None
    rating_md: int | None
    rating_sofascore: float | None
    points_as: int | None
    points_marca: int | None
    points_md: int | None
    points_sofascore: int | None
    points_mix: int | None
    points_final: int | None
    goals: int
    penalty_goals: int
    own_goals: int
    assists: int
    yellow_cards: int
    double_yellow: int
    red_cards: int
    missed_penalties: int
    saved_penalties: int
    sub_in_minute: int | None
    sub_out_minute: int | None
    has_manual_rating: bool
    market_value: int | None
    graded_at: datetime | None


# -- utilidades ----------------------------------------------------------------


def parse_local(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=MADRID)
    except ValueError:
        return None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _float(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None
    return None


def _require(data: object, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise NormalizeError(f"{what}: se esperaba un objeto")
    return data


# -- /ajax/sw/gameweek -----------------------------------------------------------


def gameweeks_from(gw_data: object) -> list[GameweekRow]:
    data = _require(gw_data, "gameweek")
    rows = []
    for g in data.get("gameweeks") or []:
        rows.append(
            GameweekRow(
                id=int(g["id"]),
                number=int(g["gameweek"]),
                season=g.get("season"),
                type=g.get("type"),
                status=g.get("status"),
                first_match_at=parse_local(g.get("firstMatchDate")),
                last_match_at=parse_local(g.get("lastMatchDate")),
            )
        )
    return rows


def fixtures_from(gw_data: object) -> list[FixtureRow]:
    data = _require(gw_data, "gameweek")
    rows = []
    for g in data.get("games") or []:
        ts = (g.get("date") or {}).get("ts")
        rows.append(
            FixtureRow(
                id=int(g["id"]),
                gameweek_id=int(g["id_gameweek"]),
                home_team_id=int(g["id_home"]),
                away_team_id=int(g["id_away"]),
                kickoff_at=datetime.fromtimestamp(ts, MADRID) if isinstance(ts, int) else None,
                status=g.get("status"),
                goals_home=_int(g.get("goals_home")),
                goals_away=_int(g.get("goals_away")),
                sofascore_id=_int(g.get("id_sofa")),
            )
        )
    return rows


def teams_from_fixtures(gw_data: object) -> dict[int, str]:
    data = _require(gw_data, "gameweek")
    teams: dict[int, str] = {}
    for g in data.get("games") or []:
        teams[int(g["id_home"])] = str(g["home"])
        teams[int(g["id_away"])] = str(g["away"])
    return teams


def graded_match_ids(gw_data: object) -> set[int]:
    """Partidos jugados y ya puntuados en Mixta (su desglose es definitivo)."""
    data = _require(gw_data, "gameweek")
    return {
        int(g["id"])
        for g in data.get("games") or []
        if g.get("status") == "played" and g.get("mixtos_graded_date")
    }


def played_players_from(gw_data: object) -> list[PlayedPlayer]:
    """Jugadores con `played` en partidos ya puntuados de la jornada.

    Sirve también para jornadas en curso (p. ej. con un partido aplazado): los
    partidos sin puntuar se ignoran y se recogerán en una ejecución posterior.
    """
    data = _require(gw_data, "gameweek")
    graded = graded_match_ids(data)
    gameweek_id = int(_require(data.get("gameweekStatus"), "gameweekStatus")["id"])
    players = data.get("players") or {}
    if not isinstance(players, dict):
        return []
    out: list[PlayedPlayer] = []
    for match_id, match in players.items():
        for team_players in (match.get("all") or {}).values():
            for p in team_players:
                if not p.get("played") or int(p.get("id_match") or match_id) not in graded:
                    continue
                out.append(
                    PlayedPlayer(
                        player=PlayerRow(
                            mister_player_id=int(p["id"]),
                            name=str(p["name"]),
                            short_name=p.get("short"),
                            slug=None,
                            position=_int(p.get("position")),
                            team_id=_int(p.get("id_team")),
                        ),
                        match_id=int(p.get("id_match") or match_id),
                        gameweek_id=gameweek_id,
                        points=_int(p.get("points")),
                        events=tuple(p.get("events") or ()),
                    )
                )
    return out


# -- /ajax/player-gameweek -------------------------------------------------------


def _event_counts(
    events: tuple[dict[str, Any], ...],
) -> tuple[Counter[str], int | None, int | None]:
    counts: Counter[str] = Counter(str(e.get("category")) for e in events)
    sub_in = next((_int(e.get("minute")) for e in events if e.get("category") == "sub_in"), None)
    sub_out = next((_int(e.get("minute")) for e in events if e.get("category") == "sub_out"), None)
    return counts, sub_in, sub_out


def _json_field(data: dict[str, Any], key: str) -> dict[str, Any]:
    raw = data.get(key)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def player_gameweek_row(
    pgw_data: object, events: tuple[dict[str, Any], ...] = ()
) -> PlayerGameweekRow:
    """Combina el desglose por fuente con los eventos de la jornada.

    Los eventos (/ajax/sw/gameweek) son la fuente de goles, penaltis y tarjetas:
    un gol de penalti llega como `penalty` (no también como `goal`) y una doble
    amarilla como `yellow` + `double`.
    """
    d = _require(pgw_data, "player-gameweek")
    counts, sub_in, sub_out = _event_counts(events)
    detailed = _json_field(d, "marca_stats_rating_detailed_filtered")
    stats = _json_field(d, "stats")
    minutes = _int((detailed.get("minutesPlayed") or {}).get("value"))
    if minutes is None:
        minutes = _int(stats.get("minutesPlayed"))
    manual = any(
        _int(d.get(k)) not in (None, 0)
        for k in ("rating_as_manual", "rating_marca_manual", "rating_md_manual", "rating_ss_manual")
    )
    graded = [
        parse_local(d.get(k)) for k in ("mix_graded_date", "mr_graded_date", "as_graded_date")
    ]
    return PlayerGameweekRow(
        player_id=int(d.get("id_player") or d["id"]),
        gameweek_id=int(d["id_gameweek"]),
        match_id=_int(d.get("id_match")),
        team_id=_int(d.get("match_team_id") or d.get("id_team")),
        position=_int(d.get("position")),
        minutes=minutes,
        rating_as=_int(d.get("rating_as")),
        rating_marca=_int(d.get("rating_marca")),
        rating_md=_int(d.get("rating_md")),
        rating_sofascore=_float(d.get("rating_ss")),
        points_as=_int(d.get("points_as")),
        points_marca=_int(d.get("points_marca")),
        points_md=_int(d.get("points_md")),
        points_sofascore=_int(d.get("points_mr")),
        points_mix=_int(d.get("points_mix")),
        points_final=_int(d.get("points")),
        goals=counts["goal"],
        penalty_goals=counts["penalty"],
        own_goals=counts["own_goal"],
        assists=counts["assist"],
        yellow_cards=counts["yellow"],
        double_yellow=counts["double"],
        red_cards=counts["red"],
        missed_penalties=counts["missed_penalty"],
        saved_penalties=counts["saved_penalty"],
        sub_in_minute=sub_in,
        sub_out_minute=sub_out,
        has_manual_rating=manual,
        market_value=_int(d.get("value")),
        graded_at=next((g for g in graded if g is not None), None),
    )


@dataclass(frozen=True)
class MatchStatsRow(Row):
    player_id: int
    fixture_id: int
    gameweek_id: int
    team_id: int | None
    minutes: int | None
    xg: float
    xa: float
    shots: int
    shots_on_target: int
    key_passes: int
    touches: int | None


def match_stats_row(pgw_data: object) -> MatchStatsRow | None:
    """xG, xA, tiros y minutos (SofaScore vía Mister). Sustituye a Understat.

    SofaScore omite las claves que valen 0 (p. ej. `expectedGoals` sin tiros).
    """
    d = _require(pgw_data, "player-gameweek")
    stats = _json_field(d, "stats")
    match_id = _int(d.get("id_match"))
    if match_id is None or not stats:
        return None
    return MatchStatsRow(
        player_id=int(d.get("id_player") or d["id"]),
        fixture_id=match_id,
        gameweek_id=int(d["id_gameweek"]),
        team_id=_int(d.get("match_team_id") or d.get("id_team")),
        minutes=_int(stats.get("minutesPlayed")),
        xg=_float(stats.get("expectedGoals")) or 0.0,
        xa=_float(stats.get("expectedAssists")) or 0.0,
        shots=_int(stats.get("totalShots")) or 0,
        shots_on_target=_int(stats.get("onTargetScoringAttempt")) or 0,
        key_passes=_int(stats.get("keyPass")) or 0,
        touches=_int(stats.get("touches")),
    )


# -- /ajax/sw/users --------------------------------------------------------------


@dataclass(frozen=True)
class UserSnapshot:
    manager: ManagerRow
    snapshot: ManagerSnapshotRow
    squad: list[SquadSnapshotRow]
    players: list[PlayerRow]


def user_snapshot(
    user_data: object,
    *,
    snapshot_date: date,
    slug: str | None,
    is_me: bool,
    standing: StandingRow | None = None,
) -> UserSnapshot:
    d = _require(user_data, "users")
    manager_id = int(d["id"])
    info = d.get("userInfo") or {}
    lineup_ids = {
        int(p["id"])
        for slots in ((d.get("lineup") or {}).get("positions") or {}).values()
        for p in (slots.values() if isinstance(slots, dict) else slots)
    }
    squad: list[SquadSnapshotRow] = []
    players: list[PlayerRow] = []
    for p in d.get("team_now") or []:
        pid = int(p["id"])
        clause = p.get("clause") or {}
        players.append(
            PlayerRow(
                mister_player_id=pid,
                name=str(p["name"]),
                short_name=p.get("short"),
                slug=None,
                position=_int(p.get("position")),
                team_id=_int(p.get("id_team")),
            )
        )
        squad.append(
            SquadSnapshotRow(
                player_id=pid,
                snapshot_date=snapshot_date,
                manager_id=manager_id,
                market_value=_int(p.get("value")),
                clause_value=_int(clause.get("value")),
                clause_floor=_int(clause.get("floor") or p.get("floor")),
                clause_multiplier=_float(clause.get("multiplier") or p.get("multiplier")),
                clause_shield=_int(clause.get("shield")),
                transfer_origin=p.get("transfer_origin"),
                on_sale_price=_int(p.get("price")),
                in_lineup=pid in lineup_ids,
            )
        )
    season = d.get("season") or {}
    value = d.get("value") or {}
    return UserSnapshot(
        manager=ManagerRow(
            mister_manager_id=manager_id,
            community_id=int(info.get("id_community") or 0),
            name=str(info.get("name") or (standing.name if standing else manager_id)),
            slug=slug,
            is_me=is_me,
        ),
        snapshot=ManagerSnapshotRow(
            manager_id=manager_id,
            snapshot_date=snapshot_date,
            season_rank=_int(season.get("rank")) or (standing.rank if standing else None),
            season_points=_int(season.get("points")),
            team_value=_int(value.get("value")),
            team_value_prev=_int(value.get("prev_value")),
            squad_size=len(squad),
        ),
        squad=squad,
        players=players,
    )


# -- /market ---------------------------------------------------------------------


def market_rows(rows: list[MarketRow], snapshot_date: date) -> list[MarketSnapshotRow]:
    return [
        MarketSnapshotRow(
            player_id=r.player_id,
            snapshot_date=snapshot_date,
            market_value=r.market_value,
            value_trend=r.value_trend,
            points=r.points,
            avg_points=r.avg_points,
            on_sale=True,
            sale_price=r.sale_price,
            seller_manager_id=r.seller_manager_id,
            sale_ends_at=r.sale_ends_at,
        )
        for r in rows
    ]


def market_players(rows: list[MarketRow]) -> list[PlayerRow]:
    return [
        PlayerRow(
            mister_player_id=r.player_id,
            name=r.short_name,
            short_name=r.short_name,
            slug=r.slug,
            position=r.position,
            team_id=r.team_id,
        )
        for r in rows
    ]


# -- /ajax/feed ------------------------------------------------------------------


def league_events_from(feed_cards: list[dict[str, Any]]) -> list[LeagueEventRow]:
    """Tarjetas de mercado del feed. Se ignoran porras y posts."""
    rows: list[LeagueEventRow] = []
    for card in feed_cards:
        category = str(card.get("category"))
        if category not in ("transfer", "clauses_drops"):
            continue
        occurred = parse_local(card.get("created"))
        if occurred is None:
            continue
        card_id = int(card["id"])
        for i, item in enumerate(card.get("data") or []):
            if category == "transfer":
                key = f"transfer:{item.get('id_transfer') or f'{card_id}:{i}'}"
                player_id = _int(item.get("id"))
                from_id = _int(item.get("id_uc_from"))
                to_id = _int(item.get("id_uc_to"))
                price = _int(item.get("price"))
                event_type = item.get("type")
            else:
                key = f"clauses_drops:{card_id}:{i}"
                player_id = _int(item.get("id"))
                from_id = to_id = None
                price = _int(item.get("floor"))
                event_type = "clause_drop"
            rows.append(
                LeagueEventRow(
                    event_key=key,
                    community_id=int(card.get("id_community") or 0),
                    feed_card_id=card_id,
                    category=category,
                    event_type=event_type,
                    occurred_at=occurred,
                    player_id=player_id,
                    from_manager_id=from_id or None,
                    to_manager_id=to_id or None,
                    price=price,
                    payload=json.dumps(item, ensure_ascii=False),
                )
            )
    return rows
