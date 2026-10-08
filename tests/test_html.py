from __future__ import annotations

from pathlib import Path

import pytest

from mister_assistant.sources.mister_html import MisterParseError, parse_market, parse_standings

FIX = Path(__file__).parent / "fixtures" / "mister"


def test_parse_standings() -> None:
    rows = parse_standings((FIX / "standings.html").read_text(encoding="utf-8"))
    assert len(rows) == 14
    assert [r.rank for r in rows] == sorted(r.rank for r in rows)
    me = [r for r in rows if r.is_me]
    assert len(me) == 1 and me[0].team_value == 65_600_000 and me[0].points == 206
    assert rows[0].points == 292 and rows[0].squad_size == 13


def test_parse_market() -> None:
    rows = parse_market((FIX / "market.html").read_text(encoding="utf-8"))
    assert len(rows) == 4
    first = rows[0]
    assert (first.player_id, first.position, first.team_id) == (3060, 4, 48)
    assert first.sale_price == 5_531_000 and first.market_value == 5_571_000
    assert first.value_trend == 1 and first.avg_points == 6.1
    assert first.sale_ends_at is not None


def test_market_sold_by_the_game_has_no_seller() -> None:
    html = (FIX / "market.html").read_text(encoding="utf-8")
    html = html.replace('data-owner="8887539"', 'data-owner="0"', 1)
    assert parse_market(html)[0].seller_manager_id is None


def test_parse_errors_are_typed() -> None:
    with pytest.raises(MisterParseError):
        parse_standings("<div>nada</div>")
    with pytest.raises(MisterParseError):
        parse_market("<div>nada</div>")
