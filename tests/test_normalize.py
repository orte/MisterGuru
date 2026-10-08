from __future__ import annotations

from datetime import date

from conftest import load_fixture

from mister_assistant.store import normalize as nz


def test_gameweek_finished() -> None:
    data = load_fixture("mister/gameweek_finished.json")["data"]
    gws = nz.gameweeks_from(data)
    assert len(gws) == 38 and gws[0].number == 1 and gws[0].status == "finished"
    fixtures = nz.fixtures_from(data)
    assert len(fixtures) == 2 and all(f.status == "played" for f in fixtures)
    assert fixtures[0].kickoff_at is not None and fixtures[0].kickoff_at.tzinfo is not None
    played = nz.played_players_from(data)
    assert played and all(p.gameweek_id == 4048 for p in played)
    assert {p.match_id for p in played} == {f.id for f in fixtures}
    teams = nz.teams_from_fixtures(data)
    assert len(teams) == 4


def test_ungraded_matches_are_skipped() -> None:
    data = load_fixture("mister/gameweek_finished.json")["data"]
    first, second = data["games"]
    first["status"], first["mixtos_graded_date"] = "fixture", None  # aplazado
    played = nz.played_players_from(data)
    assert played and {p.match_id for p in played} == {second["id"]}
    assert nz.graded_match_ids(data) == {second["id"]}


def test_player_gameweek_with_events() -> None:
    data = load_fixture("mister/player_gameweek_59789.json")["data"]
    events = (
        {"category": "sub_in", "minute": 61},
        {"category": "yellow", "minute": 96},
        {"category": "double", "minute": 96},
    )
    row = nz.player_gameweek_row(data, events)
    assert (row.player_id, row.gameweek_id, row.position) == (59789, 4048, 4)
    assert (row.rating_as, row.rating_marca, row.rating_md, row.rating_sofascore) == (0, 1, 1, 6.0)
    assert (row.points_mix, row.points_final) == (-3, -3)
    assert (row.double_yellow, row.yellow_cards, row.sub_in_minute) == (1, 1, 61)
    assert row.minutes == 35
    assert row.graded_at is not None


def test_user_snapshot() -> None:
    data = load_fixture("mister/user.json")["data"]
    snap = nz.user_snapshot(data, snapshot_date=date(2026, 10, 8), slug="manager-yo", is_me=True)
    assert snap.manager.is_me and snap.manager.community_id == 1277524
    assert snap.snapshot.team_value is not None and snap.snapshot.squad_size == 3
    first = snap.squad[0]
    assert first.clause_value is not None and first.manager_id == snap.manager.mister_manager_id
    assert any(s.in_lineup for s in snap.squad) or snap.squad


def test_league_events_from_feed() -> None:
    cards = load_fixture("mister/feed_page.json")["data"]
    events = nz.league_events_from(cards)
    assert events
    transfer = next(e for e in events if e.category == "transfer")
    assert transfer.event_key.startswith("transfer:") and transfer.price is not None
    assert transfer.occurred_at.tzinfo is not None
    assert len({e.event_key for e in events}) == len(events)
