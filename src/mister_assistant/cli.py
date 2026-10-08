"""Punto de entrada `mister-assistant`."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from mister_assistant.config import ConfigError, Settings, load_settings
from mister_assistant.delivery.telegram import TelegramError, send_message
from mister_assistant.jobs.common import JobResult
from mister_assistant.jobs.doctor import EXIT_SESSION_EXPIRED, format_report, run_doctor
from mister_assistant.log import setup_logging

log = logging.getLogger("mister_assistant")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    setup_logging(settings.log_level, settings.secret_values())

    try:
        match args.command:
            case "doctor":
                return _doctor(settings, notify=args.notify)
            case "migrate":
                return _migrate(settings)
            case "snapshot-daily":
                return _snapshot(settings, force=args.force)
            case "backfill-gameweeks":
                return _backfill(settings, args.max_requests, args.gameweek)
            case "calibrate":
                return _calibrate(settings, args.target)
            case "notify":
                return _notify(settings, " ".join(args.text))
    except ConfigError as exc:
        print(f"✗ Configuración: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return EXIT_FAILED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mister-assistant")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Valida credenciales de Mister y lista las ligas")
    doctor.add_argument("--notify", action="store_true", help="Avisa por Telegram si falla")
    sub.add_parser("migrate", help="Aplica las migraciones SQL pendientes")
    snap = sub.add_parser("snapshot-daily", help="Foto diaria de la liga (idempotente por día)")
    snap.add_argument("--force", action="store_true", help="Repite aunque ya haya una ok hoy")
    back = sub.add_parser("backfill-gameweeks", help="Desglose por fuente de jornadas cerradas")
    back.add_argument("--max-requests", type=int, default=None, help="Corta tras N peticiones")
    back.add_argument("--gameweek", type=int, action="append", help="Número de jornada (repetible)")
    cal = sub.add_parser("calibrate", help="Compara el motor de puntuación con Mister")
    cal.add_argument("--target", choices=["points_mix", "points_final"], default="points_mix")
    note = sub.add_parser("notify", help="Envía un mensaje por Telegram")
    note.add_argument("text", nargs="+")
    return parser


def _doctor(settings: Settings, *, notify: bool) -> int:
    report = run_doctor(settings)
    text = format_report(report)
    print(text)
    if notify and report.exit_code != 0:
        _send(settings, "⚠️ Mister doctor\n" + text)
    return report.exit_code


def _migrate(settings: Settings) -> int:
    from mister_assistant.store.db import connect, migrate

    with connect(settings.database_dsn()) as conn:
        applied = migrate(conn)
    print("Migraciones aplicadas: " + (", ".join(applied) if applied else "ninguna (al día)"))
    return EXIT_OK


def _snapshot(settings: Settings, *, force: bool) -> int:
    from mister_assistant.jobs.snapshot_daily import run_snapshot_daily
    from mister_assistant.sources.mister import MisterClient
    from mister_assistant.store.db import connect

    dsn = settings.database_dsn()
    with connect(dsn) as conn, MisterClient(settings) as client:
        result = run_snapshot_daily(conn, client, force=force)
    return _report_job(settings, result)


def _backfill(settings: Settings, max_requests: int | None, numbers: list[int] | None) -> int:
    from mister_assistant.jobs.backfill_gameweeks import run_backfill
    from mister_assistant.sources.mister import MisterClient
    from mister_assistant.store.db import connect

    dsn = settings.database_dsn()
    with connect(dsn) as conn, MisterClient(settings) as client:
        result = run_backfill(conn, client, max_requests=max_requests, numbers=numbers)
    return _report_job(settings, result, notify_partial=False)


def _calibrate(settings: Settings, target: str) -> int:
    from mister_assistant.jobs.calibrate import run_calibration
    from mister_assistant.scoring.calibration import format_calibration
    from mister_assistant.store.db import connect

    with connect(settings.database_dsn()) as conn:
        report = run_calibration(conn, target=target)
    print(format_calibration(report))
    return EXIT_OK


def _notify(settings: Settings, text: str) -> int:
    return EXIT_OK if _send(settings, text) else EXIT_FAILED


def _report_job(settings: Settings, result: JobResult, *, notify_partial: bool = True) -> int:
    print(result.summary())
    if result.status == "failed" or (result.status == "partial" and notify_partial):
        icon = "🔑" if result.session_expired else "⚠️"
        hint = "\nRenueva las credenciales de Mister (README)." if result.session_expired else ""
        _send(settings, f"{icon} {result.summary()}{hint}")
    if result.session_expired:
        return EXIT_SESSION_EXPIRED
    return EXIT_FAILED if result.status == "failed" else EXIT_OK


def _send(settings: Settings, text: str) -> bool:
    try:
        return send_message(settings, text)
    except TelegramError as exc:
        log.error("no se pudo avisar por Telegram: %s", exc)
        return False


if __name__ == "__main__":
    sys.exit(main())
