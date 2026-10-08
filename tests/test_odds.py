from __future__ import annotations

import httpx
import pytest

from mister_assistant.sources.http import SourceError
from mister_assistant.sources.odds import OddsClient, consensus

EVENT = {
    "id": "ev1",
    "commence_time": "2026-10-11T16:30:00Z",
    "home_team": "Real Betis",
    "away_team": "CA Osasuna",
    "bookmakers": [
        {"key": "a", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Real Betis", "price": 2.0}, {"name": "Draw", "price": 3.5},
                {"name": "CA Osasuna", "price": 4.0}]},
            {"key": "totals", "outcomes": [
                {"name": "Over", "price": 1.9, "point": 2.5},
                {"name": "Under", "price": 1.9, "point": 2.5}]},
        ]},
        {"key": "b", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Real Betis", "price": 2.1}, {"name": "Draw", "price": 3.4},
                {"name": "CA Osasuna", "price": 3.8}]},
        ]},
    ],
}  # fmt: skip


def test_consensus_removes_margin_and_averages() -> None:
    (ev,) = consensus([EVENT])
    lines = {(ln.market, ln.outcome, ln.point): ln for ln in ev.lines}
    h2h = [lines[("h2h", o, 0.0)] for o in ("home", "draw", "away")]
    assert sum(ln.prob_fair for ln in h2h) == pytest.approx(1.0, abs=1e-3)
    assert h2h[0].bookmakers == 2 and h2h[0].price_avg == pytest.approx(2.05)
    over = lines[("totals", "over", 2.5)]
    assert over.prob_fair == pytest.approx(0.5) and over.bookmakers == 1
    assert ev.commence_at.tzinfo is not None


def test_client_reads_quota_and_hides_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json=[EVENT], headers={"x-requests-remaining": "498", "x-requests-last": "2"}
        )

    with OddsClient("clave-secreta", transport=httpx.MockTransport(handler)) as client:
        assert len(client.odds()) == 1
        assert client.quota.remaining == 498 and client.quota.last_cost == 2
    assert seen[0].url.params["markets"] == "h2h,totals"

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with (
        OddsClient("clave-secreta", transport=httpx.MockTransport(unauthorized)) as client,
        pytest.raises(SourceError) as exc,
    ):
        client.odds()
    assert "clave-secreta" not in str(exc.value)
