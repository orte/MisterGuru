from __future__ import annotations

from mister_assistant.decide.lineup import FORMATIONS, Option, best_lineup, evaluate, recommend


def squad() -> list[Option]:
    out = [Option(1, "POR1", 1, 4.0, 0.95), Option(2, "POR2", 1, 1.0, 0.1)]
    out += [Option(10 + i, f"DEF{i}", 2, 3.0 - i * 0.2, 0.9) for i in range(6)]
    out += [Option(20 + i, f"CEN{i}", 3, 4.0 - i * 0.3, 0.9) for i in range(6)]
    out += [Option(30 + i, f"DEL{i}", 4, 6.0 - i * 1.0, 0.9) for i in range(4)]
    return out


def brute_force(sq: list[Option]) -> float:
    best = float("-inf")
    for f in FORMATIONS:
        d, m, a = (int(x) for x in f.split("-"))
        need = {1: 1, 2: d, 3: m, 4: a}
        total = 0.0
        ok = True
        for pos, n in need.items():
            vals = sorted((o.exp_points for o in sq if o.position == pos), reverse=True)
            if len(vals) < n:
                ok = False
                break
            total += sum(vals[:n])
        if ok:
            best = max(best, total)
    return best


def test_picks_optimal_formation() -> None:
    sq = squad()
    lineup = best_lineup(sq)
    assert len(lineup.players) == 11 and lineup.empty_slots == 0
    assert sum(1 for p in lineup.players if p.position == 1) == 1
    assert lineup.formation in FORMATIONS
    assert abs(lineup.expected_points - brute_force(sq)) < 1e-9


def test_never_leaves_gaps_if_avoidable() -> None:
    # Solo 3 defensas: tiene que elegir una formación de 3 atrás aunque sume menos.
    sq = [o for o in squad() if not (o.position == 2 and o.player_id >= 13)]
    lineup = best_lineup(sq)
    assert lineup.empty_slots == 0 and lineup.formation.startswith("3-")


def test_gap_when_squad_cannot_fill() -> None:
    sq = [o for o in squad() if o.position != 1]
    lineup = best_lineup(sq)
    assert lineup.empty_slots == 1  # sin portero: un hueco, -4
    assert lineup.expected_points == sum(p.exp_points for p in lineup.players) - 4


def test_fragile_players_get_a_replacement() -> None:
    sq = squad()
    sq[14] = Option(sq[14].player_id, "DEL-dudoso", 4, 7.0, 0.4)  # el mejor delantero, dudoso
    lineup = recommend(sq)
    (fr,) = lineup.fragile
    assert fr.player_id == sq[14].player_id and fr.replacement_id is not None
    assert fr.cost > 0


def test_evaluate_current_lineup_counts_gaps() -> None:
    sq = squad()
    ids = [o.player_id for o in best_lineup(sq).players]
    assert evaluate(sq, ids) == best_lineup(sq).expected_points
    assert evaluate(sq, ids[:10]) == evaluate(sq, ids) - ids_points(sq, ids[10]) - 4


def ids_points(sq: list[Option], pid: int) -> float:
    return next(o.exp_points for o in sq if o.player_id == pid)


def test_only_free_formations() -> None:
    from mister_assistant.decide.lineup import PAID_FORMATIONS

    assert set(FORMATIONS) == {"4-4-2", "4-5-1", "4-3-3", "3-4-3", "3-5-2", "5-4-1", "5-3-2"}
    assert not set(FORMATIONS) & set(PAID_FORMATIONS)
    # Delanteros muy buenos: con las de pago saldría un 4-2-4 o un 3-3-4.
    sq = squad() + [Option(40 + i, f"Crack{i}", 4, 9.0, 0.95) for i in range(4)]
    assert best_lineup(sq).formation in FORMATIONS
