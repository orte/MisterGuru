from __future__ import annotations

import math
import random
from datetime import date, timedelta

from conftest import load_fixture

from mister_assistant.models.value import Series, fit
from mister_assistant.store.normalize import owner_events, parse_spanish_date, value_history

START = date(2025, 10, 1)


def trending_series(n_players: int, days: int, momentum: float, seed: int = 1) -> list[Series]:
    """Valores con inercia: la variación de mañana se parece a la de hoy."""
    rng = random.Random(seed)
    out = []
    for pid in range(n_players):
        v, drift = 5_000_000.0, 0.0
        vals = {}
        for d in range(days):
            drift = momentum * drift + rng.gauss(0, 0.01)
            v *= math.exp(drift)
            vals[START + timedelta(days=d)] = round(v)
        out.append(Series(pid, 3, vals))
    return out


def test_model_beats_no_change_when_values_have_momentum() -> None:
    model = fit(trending_series(40, 120, momentum=0.9), {})
    r = model.reports[7]
    assert r.n_test > 0 and r.use_model
    assert r.mae_model < r.mae_zero


def test_model_falls_back_to_no_change_on_random_walk() -> None:
    model = fit(trending_series(40, 120, momentum=0.0, seed=7), {})
    s = trending_series(1, 120, momentum=0.0, seed=8)[0]
    fc = model.predict(s, max(s.values), None)
    assert fc is not None
    if not model.reports[14].use_model:
        assert fc.change_14d == 0.0
    assert abs(fc.change_14d) < 0.1


def test_spanish_dates() -> None:
    assert parse_spanish_date("8 oct 2025") == date(2025, 10, 8)
    assert parse_spanish_date("5 sept 2026") == date(2026, 9, 5)
    assert parse_spanish_date("31 feb 2026") is None
    assert parse_spanish_date("ayer") is None


def test_value_history_and_owner_events() -> None:
    data = {
        "id": 53111,
        "player": {"id": 53111},
        "values_chart": {"points": [
            {"value": 100, "date": "1 oct 2026"}, {"value": 110, "date": "2 oct 2026"},
        ]},
        "owners": [{"id": 545466464, "from": "A", "to": "B", "id_uc_from": 1, "id_uc_to": 2,
                    "price": 12802500, "date": "5 sept 2026", "type": "clause"}],
    }  # fmt: skip
    rows = value_history(data)
    assert [(r.value_date.day, r.value) for r in rows] == [(1, 100), (2, 110)]
    (ev,) = owner_events(data, 1277524)
    assert ev.event_key == "transfer:545466464" and ev.event_type == "clause"
    assert (ev.from_manager_id, ev.to_manager_id, ev.price) == (1, 2, 12802500)
    assert ev.occurred_at.date() == date(2026, 9, 5)


def test_real_feed_and_owner_ids_share_key_space() -> None:
    from mister_assistant.store.normalize import league_events_from

    cards = load_fixture("mister/feed_page.json")["data"]
    keys = {e.event_key for e in league_events_from(cards) if e.category == "transfer"}
    assert keys and all(k.startswith("transfer:") for k in keys)
