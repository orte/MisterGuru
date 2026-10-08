"""Job `derive-match-stats`: rellena match_stats desde lo crudo ya guardado.

No hace peticiones: lee las respuestas de /ajax/player-gameweek de
raw_responses. El backfill ya rellena match_stats sobre la marcha; esto sirve
para lo cargado antes de la Fase 2 y para rehacer la tabla si cambia el parser.
"""

from __future__ import annotations

from typing import Any

import psycopg

from mister_assistant.store import normalize as nz
from mister_assistant.store import repo

BATCH = 500


def run_derive_match_stats(conn: psycopg.Connection[Any]) -> int:
    total = 0
    # Cursor de servidor (necesita transacción): no carga todo lo crudo en memoria.
    with conn.transaction(), conn.cursor(name="raw_pgw") as cur:
        cur.execute(
            "select body_json->'data' from raw_responses"
            " where source = 'mister' and route = 'player_gameweek' and body_json is not null"
        )
        while rows := cur.fetchmany(BATCH):
            stats = [s for (data,) in rows if (s := nz.match_stats_row(data)) is not None]
            unique = {(s.player_id, s.fixture_id): s for s in stats}
            repo.insert_rows(
                conn, "match_stats", list(unique.values()), ["player_id", "fixture_id"], update=True
            )
            total += len(unique)
    return total
