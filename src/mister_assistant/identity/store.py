"""Persistencia del emparejado: team_xref, player_xref e identity_review."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from mister_assistant.identity.matching import (
    Candidate,
    ExternalPlayer,
    MatchResult,
    match_player,
    match_team,
)

Conn = psycopg.Connection[tuple[Any, ...]]


def load_teams(conn: Conn) -> dict[int, str]:
    return {int(r[0]): str(r[1]) for r in conn.execute("select id, name from teams")}


def load_candidates(conn: Conn) -> list[Candidate]:
    rows = conn.execute(
        "select mister_player_id, name, short_name, team_id, position from players"
    ).fetchall()
    return [Candidate(int(r[0]), str(r[1]), r[2], r[3], r[4]) for r in rows]


def team_id_for(conn: Conn, source: str, external_id: str, name: str, teams: dict[int, str]) -> int:
    """Equipo de Mister para un equipo externo; lo guarda en team_xref la primera vez."""
    row = conn.execute(
        "select team_id from team_xref where source = %s and external_id = %s",
        (source, external_id),
    ).fetchone()
    if row is not None:
        return int(row[0])
    team_id, _, method = match_team(name, teams)
    conn.execute(
        "insert into team_xref (team_id, source, external_id, external_name, method)"
        " values (%s, %s, %s, %s, %s)"
        " on conflict (team_id, source) do update set external_id = excluded.external_id,"
        " external_name = excluded.external_name, method = excluded.method, updated_at = now()",
        (team_id, source, external_id, name, method),
    )
    return team_id


def matched_external_ids(conn: Conn, source: str) -> set[str]:
    rows = conn.execute("select external_id from player_xref where source = %s", (source,))
    return {str(r[0]) for r in rows}


def closed_review_ids(conn: Conn, source: str) -> set[str]:
    rows = conn.execute(
        "select external_id from identity_review where source = %s and status <> 'pending'",
        (source,),
    )
    return {str(r[0]) for r in rows}


def record_match(conn: Conn, source: str, ext: ExternalPlayer, result: MatchResult) -> None:
    if result.accepted:
        assert result.player_id is not None
        # Nunca pisa un emparejado verificado a mano.
        conn.execute(
            "insert into player_xref (player_id, source, external_id, external_name,"
            "  external_slug, confidence, method, verified)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s)"
            " on conflict do nothing",
            (
                result.player_id,
                source,
                ext.external_id,
                ext.name,
                ext.slug,
                result.score,
                result.method,
                result.method == "exact",
            ),
        )
        conn.execute(
            "update identity_review set status = 'resolved', updated_at = now()"
            " where source = %s and external_id = %s",
            (source, ext.external_id),
        )
        return
    conn.execute(
        "insert into identity_review (source, external_id, external_name, external_slug,"
        "  team_id, position_hint, candidates)"
        " values (%s, %s, %s, %s, %s, %s, %s)"
        " on conflict (source, external_id) do update set candidates = excluded.candidates,"
        "  team_id = excluded.team_id, updated_at = now()"
        " where identity_review.status = 'pending'",
        (
            source,
            ext.external_id,
            ext.name,
            ext.slug,
            ext.team_id,
            None if ext.goalkeeper is None else ("portero" if ext.goalkeeper else "campo"),
            Jsonb([{"player_id": p, "name": n, "score": s} for p, n, s in result.candidates]),
        ),
    )


@dataclass
class MatchStats:
    new: int = 0
    accepted: int = 0
    review: int = 0
    unmatched: int = 0


def match_externals(
    conn: Conn, source: str, players: Iterable[ExternalPlayer], candidates: list[Candidate]
) -> MatchStats:
    """Empareja los ids externos aún no emparejados ni descartados."""
    done = matched_external_ids(conn, source) | closed_review_ids(conn, source)
    stats = MatchStats()
    seen: set[str] = set()
    for ext in players:
        if ext.external_id in done or ext.external_id in seen:
            continue
        seen.add(ext.external_id)
        stats.new += 1
        result = match_player(ext, candidates)
        record_match(conn, source, ext, result)
        if result.accepted:
            stats.accepted += 1
        elif result.method == "review":
            stats.review += 1
        else:
            stats.unmatched += 1
    return stats


def rematch_pending(conn: Conn, source: str) -> MatchStats:
    """Reintenta la cola pendiente con los jugadores de Mister actuales (sin red)."""
    rows = conn.execute(
        "select external_id, external_name, external_slug, team_id, position_hint"
        " from identity_review where source = %s and status = 'pending'",
        (source,),
    ).fetchall()
    candidates = load_candidates(conn)
    stats = MatchStats()
    for r in rows:
        ext = ExternalPlayer(
            external_id=str(r[0]),
            name=str(r[1]),
            slug=r[2],
            team_id=r[3],
            goalkeeper=None if r[4] is None else r[4] == "portero",
        )
        stats.new += 1
        result = match_player(ext, candidates)
        record_match(conn, source, ext, result)
        if result.accepted:
            stats.accepted += 1
        elif result.method == "review":
            stats.review += 1
        else:
            stats.unmatched += 1
    return stats


def search_players(
    conn: Conn, query: str, team: str | None = None, limit: int = 10
) -> list[tuple[int, str, str, int | None, float]]:
    """Jugadores de Mister parecidos a `query` (id, nombre, equipo, posición, puntuación)."""
    from rapidfuzz import fuzz

    from mister_assistant.identity.names import normalize

    rows = conn.execute(
        "select p.mister_player_id, p.name, p.short_name, coalesce(t.name, ''), p.position"
        " from players p left join teams t on t.id = p.team_id"
    ).fetchall()
    q = normalize(query)
    scored = []
    for pid, name, short, team_name, pos in rows:
        if team and normalize(team) not in normalize(team_name):
            continue
        s = max(
            fuzz.token_set_ratio(q, normalize(name)),
            fuzz.token_set_ratio(q, normalize(short or "")),
        )
        scored.append((int(pid), str(name), str(team_name), pos, float(s)))
    return sorted(scored, key=lambda r: -r[4])[:limit]


# -- revisión manual (CSV) -------------------------------------------------------

REVIEW_COLUMNS = (
    "source",
    "external_id",
    "external_name",
    "team",
    "candidates",
    "sugerencia",
    "decision",
)


def export_review(conn: Conn, path: Path) -> int:
    """Escribe la cola pendiente.

    `decision` sale siempre vacía: solo lo que escriba una persona (un player_id de
    Mister o «ignorar») se aplica, y se marca como verificado. `sugerencia` es el
    mejor candidato, solo como ayuda.
    """
    rows = conn.execute(
        "select r.source, r.external_id, r.external_name, t.name, r.candidates"
        " from identity_review r left join teams t on t.id = r.team_id"
        " where r.status = 'pending' order by t.name, r.external_name"
    ).fetchall()
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(REVIEW_COLUMNS)
        for source, ext_id, name, team, cands in rows:
            text = " | ".join(f"{c['player_id']} {c['name']} ({c['score']})" for c in cands)
            suggestion = cands[0]["player_id"] if cands and cands[0]["score"] >= 70 else ""
            w.writerow([source, ext_id, name, team or "", text, suggestion, ""])
    return len(rows)


@dataclass
class ImportStats:
    linked: int = 0
    ignored: int = 0
    skipped: int = 0
    conflicts: list[str] = field(default_factory=list)


class ReviewFileError(ValueError):
    """El CSV de revisión no tiene las columnas esperadas."""


def read_review_csv(path: Path) -> list[dict[str, str]]:
    """Lee el CSV de revisión, también si se guardó desde Excel (`;`, BOM, CRLF)."""
    text = path.read_text(encoding="utf-8-sig")
    header = text.splitlines()[0] if text else ""
    delimiter = ";" if header.count(";") > header.count(",") else ","
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    missing = {"source", "external_id", "decision"} - set(reader.fieldnames or [])
    if missing:
        raise ReviewFileError(
            f"faltan columnas {sorted(missing)} en {path.name}"
            f" (cabecera leída: {reader.fieldnames})"
        )
    return [{k: (v or "").strip() for k, v in row.items() if k is not None} for row in reader]


def import_review(conn: Conn, path: Path) -> ImportStats:
    """Aplica las decisiones del CSV. Nunca sustituye un emparejado existente."""
    stats = ImportStats()
    for row in read_review_csv(path):
        decision = row.get("decision", "").lower()
        source, ext_id = row["source"], row["external_id"]
        if decision in ("ignorar", "ignore"):
            _close_review(conn, source, ext_id, "ignored")
            stats.ignored += 1
        elif decision.isdigit():
            player_id = int(decision)
            clash = conn.execute(
                "select external_id from player_xref where source = %s and player_id = %s"
                " union all select external_id from player_xref"
                " where source = %s and external_id = %s",
                (source, player_id, source, ext_id),
            ).fetchone()
            if clash is not None:
                stats.conflicts.append(
                    f"{row.get('external_name') or ext_id} → {player_id}: ya emparejado"
                    f" (id externo {clash[0]})"
                )
                continue
            conn.execute(
                "insert into player_xref (player_id, source, external_id, external_name,"
                "  confidence, method, verified)"
                " values (%s, %s, %s, %s, 100, 'manual', true)",
                (player_id, source, ext_id, row.get("external_name") or ext_id),
            )
            _close_review(conn, source, ext_id, "resolved")
            stats.linked += 1
        else:
            stats.skipped += 1
    return stats


def _close_review(conn: Conn, source: str, ext_id: str, status: str) -> None:
    conn.execute(
        "update identity_review set status = %s, updated_at = now()"
        " where source = %s and external_id = %s",
        (status, source, ext_id),
    )


# -- cobertura -------------------------------------------------------------------


@dataclass(frozen=True)
class Coverage:
    source: str
    total: int
    matched: int
    missing: list[tuple[int, str, str]]  # (player_id, nombre, equipo)

    @property
    def rate(self) -> float:
        return self.matched / self.total if self.total else 0.0


def coverage(conn: Conn, source: str, recent_gameweeks: int | None = None) -> Coverage:
    """Jugadores con minutos y equipo actual en LaLiga que están emparejados.

    Con `recent_gameweeks`, solo cuenta a quienes jugaron en las últimas N jornadas
    disputadas (los que ya no juegan no aparecen en las alineaciones probables).
    """
    since = 0
    if recent_gameweeks is not None:
        row = conn.execute(
            "select coalesce(min(number), 0) from (select distinct g.number"
            " from player_gameweek pg join gameweeks g on g.id = pg.gameweek_id"
            " order by g.number desc limit %s) t",
            (recent_gameweeks,),
        ).fetchone()
        since = int(row[0]) if row else 0
    rows = conn.execute(
        "select p.mister_player_id, p.name, coalesce(t.name, ''),"
        "  exists (select 1 from player_xref x where x.player_id = p.mister_player_id"
        "          and x.source = %s)"
        " from players p left join teams t on t.id = p.team_id"
        " where p.mister_player_id in"
        "  (select pg.player_id from player_gameweek pg join gameweeks g on g.id = pg.gameweek_id"
        "   where pg.minutes > 0 and g.number >= %s)"
        " and p.team_id in (select team_id from team_xref where source = %s)"
        " order by t.name, p.name",
        (source, since, source),
    ).fetchall()
    missing = [(int(r[0]), str(r[1]), str(r[2])) for r in rows if not r[3]]
    return Coverage(source, len(rows), len(rows) - len(missing), missing)


def to_json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
