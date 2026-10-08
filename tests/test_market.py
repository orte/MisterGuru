from __future__ import annotations

import pytest

from mister_assistant.decide import market as mk
from mister_assistant.decide.balances import rank_bonus
from mister_assistant.decide.lineup import Option


def squad() -> list[Option]:
    out = [Option(1, "POR", 1, 4.0, 0.95)]
    out += [Option(10 + i, f"DEF{i}", 2, 3.0, 0.9) for i in range(5)]
    out += [Option(20 + i, f"CEN{i}", 3, 3.0, 0.9) for i in range(5)]
    out += [Option(30 + i, f"DEL{i}", 4, 3.0, 0.9) for i in range(3)]
    return out


def test_bid_levels_from_history() -> None:
    ratios = [1.0 + i / 100 for i in range(40)]  # 1,00 … 1,39
    lv = mk.bid_levels(ratios)
    assert lv.samples == 40
    assert lv.tight == pytest.approx(1.195, abs=0.01)
    assert lv.tight < lv.likely < lv.safe
    assert lv.amounts(1_000_000)[0] % 1000 == 0
    assert mk.bid_levels([1.1, 1.2]) == mk.DEFAULT_BIDS  # pocas muestras


def test_valuation_rewards_points_and_penalises_premium() -> None:
    sq = squad()
    star = mk.Candidate(99, "Crack", 4, 8.0, 0.95, value=10_000_000, price=10_000_000,
                        change_14d=0.05)  # fmt: skip
    v = mk.value(sq, star, balance=20_000_000)
    assert v.marginal_points == pytest.approx(5.0)
    assert v.replaces and v.affordable_now and v.score > 0
    pricey = mk.Candidate(99, "Crack", 4, 8.0, 0.95, value=10_000_000, price=40_000_000,
                          change_14d=0.05)  # fmt: skip
    v2 = mk.value(sq, pricey, balance=20_000_000)
    assert v2.score < v.score and not v2.affordable_now and v2.needs_sales == 20_000_000
    useless = mk.Candidate(98, "Suplente", 2, 1.0, 0.5, value=1_000_000, price=1_000_000,
                           change_14d=0.0)  # fmt: skip
    assert mk.value(sq, useless, balance=5_000_000).marginal_points == 0


def test_clause_raise_costs() -> None:
    steps = mk.clause_raises(floor=10_000_000, current_clause=15_000_000)
    assert [(r.step, r.new_clause, r.cost) for r in steps] == [
        (1, 20_000_000, 2_000_000), (2, 25_000_000, 4_000_000), (3, 30_000_000, 6_000_000),
    ]  # fmt: skip
    assert [r.step for r in mk.clause_raises(10_000_000, 22_000_000)] == [2, 3]


def test_exposure_picks_cheapest_sufficient_raise() -> None:
    sq = squad()
    key = Option(30, "DEL0", 4, 7.0, 0.95)
    sq = [key if o.player_id == 30 else o for o in sq] + [Option(40, "DEL-suplente", 4, 1.0, 0.5)]
    ex = mk.exposure(key, 15_000_000, 10_000_000, sq, [(7, "Rival", 22_000_000)])
    # Sin él, el once cambia de formación y entra un medio de 3 (no el suplente de 1).
    assert ex is not None and ex.marginal_loss == pytest.approx(4.0)
    assert ex.raise_to is not None and ex.raise_to.step == 2  # 25 M > 22 M
    assert mk.exposure(key, 15_000_000, 10_000_000, sq, [(7, "Rival", 5_000_000)]) is None


def test_rank_bonus_with_ties() -> None:
    bonus = rank_bonus({1: 50, 2: 40, 3: 40, 4: 10}, [1, 2, 3, 4, 5])
    assert bonus == {1: 0, 2: 200_000, 3: 200_000, 4: 600_000, 5: 800_000}
