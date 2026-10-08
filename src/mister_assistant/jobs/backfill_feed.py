"""Job `backfill-feed`: el feed de la liga completo, desde el inicio de temporada.

snapshot-daily solo mira las páginas recientes (hasta ver eventos conocidos);
esto recorre todas las páginas una vez, para tener el histórico de pujas
ganadas y cláusulas (precio pagado frente a valor del jugador).
"""

from __future__ import annotations

from datetime import date
from functools import partial
from typing import Any

import psycopg

from mister_assistant.jobs.common import DatabaseLostError, JobResult, StepRunner, today_madrid
from mister_assistant.sources.mister import MisterClient, SessionExpiredError
from mister_assistant.store import normalize as nz
from mister_assistant.store import repo

JOB = "backfill_feed"
PAGE_SIZE = 20
Conn = psycopg.Connection[tuple[Any, ...]]


def run_backfill_feed(
    conn: Conn, client: MisterClient, *, max_pages: int = 80, run_date: date | None = None
) -> JobResult:
    run_date = run_date or today_madrid()
    result = JobResult(JOB, run_date)
    run_id = repo.start_job(conn, JOB, run_date)
    steps = StepRunner(conn, result)
    result.stats = {"páginas": 0, "eventos nuevos": 0}
    try:
        for page in range(max_pages):
            cards = steps.run(f"página {page}", partial(_page, conn, client, page, run_date))
            if cards is None:
                break
            result.stats["páginas"] += 1
            result.stats["eventos nuevos"] += cards[1]
            if cards[0] < PAGE_SIZE:
                break
        oldest = conn.execute("select min(occurred_at)::date from league_events").fetchone()
        if oldest and oldest[0]:
            result.stats["desde"] = int(oldest[0].strftime("%Y%m%d"))
        result.status = steps.final_status()
    except SessionExpiredError as exc:
        result.status = "failed"
        result.session_expired = True
        result.errors.append(f"sesión caducada: {exc}")
    except DatabaseLostError as exc:
        steps.abort_on_db_loss(exc)
    finally:
        result.requests = client.requests
        result.close(conn, run_id)
    return result


def _page(conn: Conn, client: MisterClient, page: int, run_date: date) -> tuple[int, int]:
    resp = client.call(
        "feed", offset=page * PAGE_SIZE, cardsPerPage=PAGE_SIZE, end=False, loading=True
    )
    repo.save_raw(conn, resp, run_date)
    cards = resp.data if isinstance(resp.data, list) else []
    events = nz.league_events_from(cards)
    known = repo.existing_event_keys(conn, [e.event_key for e in events])
    new = [e for e in events if e.event_key not in known]
    repo.insert_rows(conn, "league_events", new, ["event_key"], json_columns=["payload"])
    return len(cards), len(new)
