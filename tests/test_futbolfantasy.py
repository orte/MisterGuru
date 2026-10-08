from __future__ import annotations

from pathlib import Path

import pytest

from mister_assistant.sources.futbolfantasy import (
    FutbolFantasyParseError,
    parse_index,
    parse_match,
    raw_players,
)

FIX = Path(__file__).parent / "fixtures" / "futbolfantasy"


def test_parse_index_current_gameweek() -> None:
    matches = parse_index((FIX / "index.html").read_text(encoding="utf-8"))
    assert len(matches) == 10
    assert {m.jornada for m in matches} == {8}
    betis = next(m for m in matches if m.match_id == "22499")
    assert (betis.home_name, betis.away_name) == ("Betis", "Osasuna")
    assert (betis.home_team_id, betis.away_team_id) == ("4", "13")
    assert betis.path == "/partidos/22499-betis-osasuna"


def test_parse_match_players() -> None:
    players = parse_match((FIX / "match_22499.html").read_text(encoding="utf-8"))
    assert len(players) == 14
    roca = players[0]
    assert (roca.player_id, roca.name, roca.slug) == ("3215", "Marc Roca", "marc-roca")
    assert (roca.side, roca.role, roca.probability, roca.injury_code) == (
        "local", "titular", 0.5, -1,
    )  # fmt: skip
    bartra = next(p for p in players if p.name == "Marc Bartra")
    assert bartra.role == "suplente" and bartra.injury_code == 2
    assert next(p for p in players if p.name == "Facundo Bernal").slug is None
    assert {p.side for p in players} == {"local", "visitante"}


def test_raw_players_keeps_attributes() -> None:
    raw = raw_players((FIX / "match_22499.html").read_text(encoding="utf-8"))
    assert len(raw) == 14 and raw[0]["name"] == "Marc Roca"
    assert raw[0]["data-probabilidad"] == "50%"


def test_parse_errors_are_typed() -> None:
    with pytest.raises(FutbolFantasyParseError):
        parse_index("<html><title>x</title></html>")
    with pytest.raises(FutbolFantasyParseError):
        parse_match("<html><body>nada</body></html>")
