"""Job `calibrate`: recalcula player_gameweek con el motor y lo compara con Mister."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import psycopg
from psycopg.rows import dict_row

from mister_assistant.scoring.calibration import CalibrationReport, calibrate
from mister_assistant.store.normalize import PlayerGameweekRow

_FIELDS = [f.name for f in fields(PlayerGameweekRow)]


def load_player_gameweeks(conn: psycopg.Connection[Any]) -> list[PlayerGameweekRow]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"select {', '.join(_FIELDS)} from player_gameweek")
        rows = cur.fetchall()
    out = []
    for r in rows:
        if r["rating_sofascore"] is not None:
            r["rating_sofascore"] = float(r["rating_sofascore"])
        out.append(PlayerGameweekRow(**r))
    return out


def run_calibration(conn: psycopg.Connection[Any], target: str = "points_mix") -> CalibrationReport:
    return calibrate(load_player_gameweeks(conn), target=target)
