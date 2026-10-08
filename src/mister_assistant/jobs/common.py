"""Utilidades comunes de los jobs: fecha de ejecución y pasos con fallo aislado."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, TypeVar

import psycopg

from mister_assistant.sources.mister import MisterError, SessionExpiredError
from mister_assistant.store.normalize import MADRID, NormalizeError

log = logging.getLogger(__name__)

T = TypeVar("T")
Status = Literal["ok", "partial", "failed", "skipped"]


def today_madrid(now: datetime | None = None) -> date:
    return (now or datetime.now(MADRID)).astimezone(MADRID).date()


@dataclass
class JobResult:
    job: str
    run_date: date
    status: Status = "ok"
    requests: int = 0
    errors: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    session_expired: bool = False

    def close(self, conn: psycopg.Connection[tuple[object, ...]], run_id: int) -> None:
        """Cierra el registro en job_runs, si la conexión sigue viva."""
        if conn.closed or conn.broken:
            log.error("no se pudo cerrar job_runs %s: conexión perdida", run_id)
            return
        from mister_assistant.store import repo

        try:
            repo.finish_job(conn, run_id, self.status, self.requests, self.errors)
        except psycopg.Error as exc:
            log.error("no se pudo cerrar job_runs %s: %s", run_id, type(exc).__name__)

    def summary(self) -> str:
        parts = [f"{self.job} {self.run_date}: {self.status}", f"{self.requests} peticiones"]
        parts += [f"{k} {v}" for k, v in self.stats.items()]
        text = " · ".join(parts)
        if self.errors:
            text += "\n" + "\n".join(f"  ✗ {e}" for e in self.errors)
        return text


class DatabaseLostError(Exception):
    """Se perdió la conexión con Postgres: no tiene sentido seguir con más pasos."""


class StepRunner:
    """Ejecuta pasos independientes: el fallo de uno se registra y el job sigue.

    Excepciones: la sesión caducada y la conexión a la BD perdida abortan el job
    (todos los pasos siguientes fallarían igual).
    """

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]], result: JobResult) -> None:
        self._conn = conn
        self._result = result
        self.failed_steps = 0
        self.steps = 0

    def run(self, name: str, fn: Callable[[], T]) -> T | None:
        self.steps += 1
        try:
            with self._conn.transaction():
                return fn()
        except SessionExpiredError:
            raise
        except psycopg.OperationalError as exc:
            if self._conn.closed or self._conn.broken:
                raise DatabaseLostError(f"{name}: {exc}".splitlines()[0]) from exc
            self._fail(name, exc)
        except (MisterError, NormalizeError, psycopg.Error, KeyError, TypeError, ValueError) as exc:
            self._fail(name, exc)
        return None

    def _fail(self, name: str, exc: Exception) -> None:
        self.failed_steps += 1
        msg = f"{name}: {type(exc).__name__}: {exc}"
        log.warning(msg)
        self._result.errors.append(msg[:500])

    def abort_on_db_loss(self, exc: DatabaseLostError) -> None:
        self._result.status = "failed"
        self._result.errors.append(f"conexión con la base de datos perdida en {exc}")

    def final_status(self) -> Status:
        if self.failed_steps == 0:
            return "ok"
        return "failed" if self.failed_steps >= self.steps else "partial"
