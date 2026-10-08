from __future__ import annotations

import pytest

from mister_assistant.identity.matching import (
    AmbiguousTeamError,
    Candidate,
    ExternalPlayer,
    match_player,
    match_team,
)
from mister_assistant.identity.names import normalize, slug_to_name, team_key

TEAMS = {
    1: "Athletic Club", 2: "Atlético", 3: "Barcelona", 4: "Betis", 5: "Celta",
    6: "Deportivo da Coruña", 8: "Espanyol", 9: "Getafe", 12: "Levante", 13: "Málaga",
    14: "Rayo Vallecano", 15: "Real Madrid", 16: "Real Sociedad", 17: "Sevilla",
    19: "Valencia", 20: "Villarreal", 23: "Elche", 48: "Alavés", 50: "Osasuna",
    1490: "Racing de Santander",
}  # fmt: skip

FF_NAMES = ["Alavés", "Athletic", "Atlético", "Barcelona", "Betis", "Celta", "Deportivo",
            "Elche", "Espanyol", "Getafe", "Levante", "Málaga", "Osasuna", "Racing", "Rayo",
            "Real Madrid", "Real Sociedad", "Sevilla", "Valencia", "Villarreal"]  # fmt: skip
ODDS_NAMES = ["Alavés", "Athletic Bilbao", "Atlético Madrid", "Barcelona", "Real Betis",
              "Celta Vigo", "Deportivo La Coruña", "Elche CF", "Espanyol", "Getafe", "Levante",
              "Málaga", "CA Osasuna", "Racing Santander", "Rayo Vallecano", "Real Madrid",
              "Real Sociedad", "Sevilla", "Valencia", "Villarreal"]  # fmt: skip


def test_normalize() -> None:
    assert normalize("Álvaro Vallés-Pérez") == "alvaro valles perez"
    assert slug_to_name("raul-garcia-1") == "raul garcia"
    assert team_key("Real Madrid CF") == "madrid"


@pytest.mark.parametrize("names", [FF_NAMES, ODDS_NAMES])
def test_all_teams_match_uniquely(names: list[str]) -> None:
    ids = [match_team(n, TEAMS)[0] for n in names]
    assert sorted(ids) == sorted(TEAMS)


def test_ambiguous_team_is_not_guessed() -> None:
    with pytest.raises(AmbiguousTeamError):
        match_team("Real", TEAMS)


BETIS = [
    Candidate(1, "Marc Roca", "M. Roca", 4, 3),
    Candidate(2, "Isco Alarcón", "Isco", 4, 3),
    Candidate(3, "Álvaro Valles", "Á. Valles", 4, 1),
    Candidate(4, "Abde Ezzalzouli", "Abde", 4, 4),
    Candidate(5, "Raúl García", "R. García", 50, 4),
    Candidate(6, "Raúl García de Haro", "Raúl García", 50, 4),
    Candidate(7, "Adrián", "Adrián", 4, 1),
]


def test_exact_and_accent_insensitive() -> None:
    r = match_player(ExternalPlayer("x", "Marc Roca", "marc-roca", 4, False), BETIS)
    assert (r.player_id, r.method) == (1, "exact")
    # Sin tildes es el mismo nombre normalizado: exacto.
    r = match_player(ExternalPlayer("x", "Isco Alarcon", None, 4, False), BETIS)
    assert (r.player_id, r.method) == (2, "exact")
    # Variante con errata: difuso.
    r = match_player(ExternalPlayer("x", "Isco Alarkon", "isco-alarcon", 4, False), BETIS)
    assert (r.player_id, r.method) == (2, "fuzzy")


def test_short_name_subset() -> None:
    r = match_player(ExternalPlayer("x", "Abde", "abde-ezzalzouli", 4, False), BETIS)
    assert r.player_id == 4


def test_ambiguous_goes_to_review() -> None:
    r = match_player(ExternalPlayer("x", "Raúl García", "raul-garcia-1", 50, False), BETIS)
    assert r.player_id in (5, None)
    if r.player_id is None:
        assert r.method == "review" and len(r.candidates) >= 2


def test_other_team_is_not_matched() -> None:
    r = match_player(ExternalPlayer("x", "Marc Roca", None, 50, False), BETIS)
    assert r.player_id is None


def test_goalkeeper_flag_breaks_ties() -> None:
    r = match_player(ExternalPlayer("x", "Adrian San Miguel", None, 4, True), BETIS)
    assert r.candidates[0][0] == 7


SUBSETS = [
    Candidate(10, "Natan Souza", "Natan", 4, 2),
    Candidate(11, "Pedri", "Pedri", 3, 3),
    Candidate(12, "Gavi", "Gavi", 3, 3),
    Candidate(13, "Alejandro Grimaldo", "Grimaldo", 2, 2),
    Candidate(14, "Fede Valverde", "F. Valverde", 15, 3),
    Candidate(15, "Fer Niño", "Fer Niño", 23, 4),
    Candidate(16, "Dani Martínez", "D. Martínez", 2, 3),
    Candidate(17, "Dani Rodríguez", "D. Rodríguez", 2, 3),
]


@pytest.mark.parametrize(
    ("name", "team", "expected"),
    [
        ("Natan", 4, 10),
        ("Pedri González", 3, 11),
        ("Pablo Gavi", 3, 12),
        ("Álex Grimaldo", 2, 13),
        ("Federico Valverde", 15, 14),
        ("Fernando Niño", 23, 15),
    ],
)
def test_subset_and_nickname_rules(name: str, team: int, expected: int) -> None:
    r = match_player(ExternalPlayer("x", name, None, team, False), SUBSETS)
    assert r.player_id == expected, r


def test_shared_first_name_alone_is_not_enough() -> None:
    # «Dani» está en dos jugadores del mismo equipo: no se elige.
    r = match_player(ExternalPlayer("x", "Dani", None, 2, False), SUBSETS)
    assert r.player_id is None
