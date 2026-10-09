"""Punto de entrada `mister-assistant`."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from typing import Any

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
            case "capture-lineups":
                return _capture_lineups(settings, skip_mister=args.skip_mister)
            case "capture-odds":
                return _capture_odds(settings)
            case "derive-match-stats":
                return _derive_match_stats(settings)
            case "identity":
                return _identity(settings, args)
            case "backfill-values":
                return _backfill_values(settings, args.max_requests)
            case "backfill-feed":
                return _backfill_feed(settings, args.max_pages)
            case "backtest":
                return _backtest(settings, save=args.save)
            case "weekly-eval":
                return _weekly_eval(settings, dry_run=args.dry_run)
            case "ask":
                return _ask(settings, " ".join(args.question))
            case "bot":
                return _bot(settings)
            case "agent-eval":
                return _agent_eval(settings)
            case "free-market-report":
                return _free_market_report(settings, dry_run=args.dry_run, force=args.force)
            case "market-report":
                return _market_report(settings, dry_run=args.dry_run, force=args.force)
            case "gameweek-report":
                return _gameweek_report(
                    settings, auto=args.auto, dry_run=args.dry_run, refresh=not args.no_refresh
                )
    except ConfigError as exc:
        print(f"✗ Configuración: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except ValueError as exc:  # p. ej. un CSV de revisión con columnas que faltan
        print(f"✗ {exc}", file=sys.stderr)
        return EXIT_FAILED
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
    lin = sub.add_parser("capture-lineups", help="Alineaciones probables (Mister + Fútbol Fantasy)")
    lin.add_argument("--skip-mister", action="store_true", help="Solo Fútbol Fantasy")
    sub.add_parser("capture-odds", help="Cuotas 1X2 y goles (The Odds API)")
    sub.add_parser("derive-match-stats", help="Rellena match_stats desde lo crudo (sin red)")
    bv = sub.add_parser("backfill-values", help="Serie diaria de valor de un año por jugador")
    bv.add_argument("--max-requests", type=int, default=None)
    bf = sub.add_parser("backfill-feed", help="Feed de la liga completo (una vez)")
    bf.add_argument("--max-pages", type=int, default=80)
    btp = sub.add_parser("backtest", help="Backtest «como si» de v0 y v1 (sin red)")
    btp.add_argument("--save", action="store_true", help="Guarda el resultado en evaluations")
    wk = sub.add_parser("weekly-eval", help="Evaluación semanal de predicciones y mercado")
    wk.add_argument("--dry-run", action="store_true", help="Imprime sin guardar ni enviar")
    askp = sub.add_parser("ask", help="Pregunta al agente desde la terminal")
    askp.add_argument("question", nargs="+")
    sub.add_parser("bot", help="Agente por Telegram (proceso que se queda escuchando)")
    sub.add_parser("agent-eval", help="Casos de prueba del agente con el modelo real (cuesta)")
    fm = sub.add_parser("free-market-report", help="¿Merece la pena pujar por algún libre?")
    fm.add_argument("--dry-run", action="store_true", help="Imprime sin enviar ni guardar")
    fm.add_argument("--force", action="store_true", help="Repite aunque ya se enviara hoy")
    mrep = sub.add_parser("market-report", help="Informe matinal de mercado y cláusulas")
    mrep.add_argument("--dry-run", action="store_true", help="Imprime sin enviar ni guardar")
    mrep.add_argument("--force", action="store_true", help="Repite aunque ya se enviara hoy")
    rep = sub.add_parser("gameweek-report", help="Predicciones, once recomendado e informe")
    rep.add_argument(
        "--auto", action="store_true", help="Solo en las ventanas víspera / 3 h antes, una vez"
    )
    rep.add_argument("--dry-run", action="store_true", help="Imprime el informe sin enviarlo")
    rep.add_argument(
        "--no-refresh", action="store_true", help="No recaptura alineaciones ni cuotas antes"
    )
    ident = sub.add_parser("identity", help="Emparejado de jugadores entre fuentes")
    isub = ident.add_subparsers(dest="identity_command", required=True)
    cov = isub.add_parser("coverage", help="Cobertura del emparejado (criterio de la Fase 2)")
    cov.add_argument(
        "--recent", type=int, default=None, help="Solo quienes jugaron en las últimas N jornadas"
    )
    isub.add_parser("rematch", help="Reintenta la cola de revisión (sin red)")
    srch = isub.add_parser("search", help="Busca jugadores de Mister por nombre (sin red)")
    srch.add_argument("query", nargs="+")
    srch.add_argument("--team", default=None, help="Filtra por equipo (parte del nombre)")
    exp = isub.add_parser("export-review", help="Exporta la cola de revisión a CSV")
    exp.add_argument("path")
    imp = isub.add_parser("import-review", help="Aplica las decisiones del CSV")
    imp.add_argument("path")
    for p in (ident,):
        p.add_argument("--source", default="futbolfantasy")
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


def _capture_lineups(settings: Settings, *, skip_mister: bool) -> int:
    from mister_assistant.jobs.capture_lineups import run_capture_lineups
    from mister_assistant.sources import futbolfantasy as ff
    from mister_assistant.sources.mister import MisterClient
    from mister_assistant.store.db import connect

    dsn = settings.database_dsn()
    ff_client = ff.make_client()
    try:
        with connect(dsn) as conn:
            if skip_mister:
                result = run_capture_lineups(conn, None, ff_client)
            else:
                with MisterClient(settings) as mister:
                    result = run_capture_lineups(conn, mister, ff_client)
    finally:
        ff_client.close()
    return _report_job(settings, result)


def _capture_odds(settings: Settings) -> int:
    from mister_assistant.jobs.capture_odds import run_capture_odds
    from mister_assistant.sources.odds import OddsClient
    from mister_assistant.store.db import connect

    if settings.odds_api_key is None or not settings.odds_api_key.get_secret_value().strip():
        print("ODDS_API_KEY no configurada: no se piden cuotas")
        return EXIT_OK
    dsn = settings.database_dsn()
    with connect(dsn) as conn:
        recent = conn.execute(
            "select 1 from odds where captured_at > now() - interval '3 hours' limit 1"
        ).fetchone()
        if recent is not None:
            print("Hay cuotas de hace menos de 3 h: no se gastan créditos")
            return EXIT_OK
        with OddsClient(settings.odds_api_key.get_secret_value()) as client:
            result = run_capture_odds(conn, client)
    return _report_job(settings, result)


def _derive_match_stats(settings: Settings) -> int:
    from mister_assistant.jobs.derive_match_stats import run_derive_match_stats
    from mister_assistant.store.db import connect

    with connect(settings.database_dsn()) as conn:
        n = run_derive_match_stats(conn)
    print(f"match_stats: {n} filas derivadas")
    return EXIT_OK


def _identity(settings: Settings, args: argparse.Namespace) -> int:
    from pathlib import Path

    from mister_assistant.identity import store as idstore
    from mister_assistant.store.db import connect

    with connect(settings.database_dsn()) as conn:
        match args.identity_command:
            case "coverage":
                cov = idstore.coverage(conn, args.source, args.recent)
                scope = (
                    f"en las últimas {args.recent} jornadas" if args.recent else "esta temporada"
                )
                print(
                    f"{args.source}: {cov.matched}/{cov.total} jugadores con minutos {scope}"
                    f" emparejados ({cov.rate:.1%}); objetivo ≥ 98%"
                )
                for pid, name, team in cov.missing[:50]:
                    print(f"  sin emparejar: {pid} {name} ({team})")
                return EXIT_OK if cov.rate >= 0.98 else EXIT_FAILED
            case "search":
                positions = {1: "POR", 2: "DEF", 3: "CEN", 4: "DEL"}
                for pid, name, team, pos, score in idstore.search_players(
                    conn, " ".join(args.query), args.team
                ):
                    pos_label = positions.get(pos or 0, "?")
                    print(f"{pid:>8}  {name:<28} {team:<22} {pos_label}  {score:.0f}")
                return EXIT_OK
            case "rematch":
                with conn.transaction():
                    st = idstore.rematch_pending(conn, args.source)
                pending = st.review + st.unmatched
                print(f"revisados {st.new}: {st.accepted} emparejados, {pending} siguen pendientes")
            case "export-review":
                n = idstore.export_review(conn, Path(args.path))
                print(
                    f"{n} pendientes exportados a {args.path};"
                    " rellena «decision» con un player_id o «ignorar»"
                )
            case "import-review":
                with conn.transaction():
                    im = idstore.import_review(conn, Path(args.path))
                print(
                    f"enlazados {im.linked}, ignorados {im.ignored}, sin decisión {im.skipped},"
                    f" conflictos {len(im.conflicts)}"
                )
                for c in im.conflicts:
                    print(f"  ✗ {c}")
    return EXIT_OK


def _backfill_values(settings: Settings, max_requests: int | None) -> int:
    from mister_assistant.jobs.backfill_values import run_backfill_values
    from mister_assistant.sources.mister import MisterClient
    from mister_assistant.store.db import connect

    dsn = settings.database_dsn()
    with connect(dsn) as conn, MisterClient(settings) as client:
        result = run_backfill_values(conn, client, max_requests=max_requests)
    return _report_job(settings, result, notify_partial=False)


def _backfill_feed(settings: Settings, max_pages: int) -> int:
    from mister_assistant.jobs.backfill_feed import run_backfill_feed
    from mister_assistant.sources.mister import MisterClient
    from mister_assistant.store.db import connect

    dsn = settings.database_dsn()
    with connect(dsn) as conn, MisterClient(settings) as client:
        result = run_backfill_feed(conn, client, max_pages=max_pages)
    return _report_job(settings, result, notify_partial=False)


def _backtest(settings: Settings, *, save: bool) -> int:
    from dataclasses import asdict

    from psycopg.types.json import Jsonb

    from mister_assistant.evals.backtest import format_backtest, run_backtest
    from mister_assistant.store.db import connect

    with connect(settings.database_dsn()) as conn:
        report = run_backtest(conn)
        text = format_backtest(report)
        print(text)
        cmp = report.compare()
        if save and cmp is not None:
            conn.execute(
                "insert into evaluations (kind, model, metrics, summary)"
                " values ('backtest', 'v0-v1', %s, %s)",
                (Jsonb(asdict(cmp)), text),
            )
    return EXIT_OK


def _weekly_eval(settings: Settings, *, dry_run: bool) -> int:
    from mister_assistant.evals.weekly import build_weekly, save_weekly
    from mister_assistant.store.db import connect

    with connect(settings.database_dsn()) as conn:
        if dry_run:
            # La auditoría escribe `outcome`: en seco se deshace al terminar.
            with conn.transaction(force_rollback=True):
                report = build_weekly(conn)
            print(report.message)
            return EXIT_OK
        with conn.transaction():
            report = build_weekly(conn)
            save_weekly(conn, report)
    print(report.message)
    return EXIT_OK if _send(settings, report.message) else EXIT_FAILED


def _anthropic_client(settings: Settings) -> Any:
    """Cliente de Anthropic con la clave de .env (el SDK solo mira el entorno)."""
    import os

    import anthropic

    key = settings.anthropic_api_key
    if key is not None and key.get_secret_value():
        return anthropic.Anthropic(api_key=key.get_secret_value())
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return anthropic.Anthropic()
    raise ConfigError(
        "Falta ANTHROPIC_API_KEY (console.anthropic.com → API Keys) en .env o el entorno"
    )


def _ask(settings: Settings, question: str) -> int:
    from mister_assistant.agent.runner import Agent
    from mister_assistant.store.db import connect

    client = _anthropic_client(settings)
    with connect(settings.database_dsn()) as conn:
        agent = Agent(conn, client)
        answer = agent.ask(question)
        agent.log(question, answer, "terminal")
    print(answer.text)
    print(
        f"\n[run {answer.run_id} · herramientas: {', '.join(c['tool'] for c in answer.tool_calls)}"
        f" · tokens entrada {answer.usage.get('input')} (caché {answer.usage.get('cache_read')})"
        f" salida {answer.usage.get('output')}]"
    )
    if answer.untraceable:
        print(f"⚠️ cifras sin respaldo en herramientas: {answer.untraceable}")
    return EXIT_OK


def _bot(settings: Settings) -> int:
    from mister_assistant.agent.runner import Agent
    from mister_assistant.delivery.telegram_bot import run_bot
    from mister_assistant.store.db import connect

    client = _anthropic_client(settings)
    dsn = settings.database_dsn()

    def agent_factory() -> Agent:
        # Conexión nueva por mensaje: el proceso vive días y el pooler corta las ociosas.
        return Agent(connect(dsn), client, owns_conn=True)

    run_bot(settings, agent_factory)
    return EXIT_OK


def _agent_eval(settings: Settings) -> int:
    from mister_assistant.agent.evals import format_results, run_cases
    from mister_assistant.agent.runner import Agent
    from mister_assistant.store.db import connect

    client = _anthropic_client(settings)
    dsn = settings.database_dsn()
    results = run_cases(lambda: Agent(connect(dsn), client, owns_conn=True))
    print(format_results(results))
    return EXIT_OK if all(r.passed for r in results) else EXIT_FAILED


def _free_market_report(settings: Settings, *, dry_run: bool, force: bool) -> int:
    from mister_assistant.jobs import free_market_report as fmr
    from mister_assistant.store import repo
    from mister_assistant.store.db import connect

    with connect(settings.database_dsn()) as conn:
        report = fmr.build_free_market_report(conn)
        slot = f"mercado-libre-{report.report_date}"
        print(report.message)
        if dry_run:
            return EXIT_OK
        if repo.daily_report_sent(conn, slot) and not force:
            print("Ya se envió el de hoy (usa --force para repetir)")
            return EXIT_OK
        with conn.transaction():
            fmr.save(conn, report)
        delivered = _send(settings, report.message)
        with conn.transaction():
            repo.log_daily_report(conn, slot, delivered)
        return EXIT_OK if delivered else EXIT_FAILED


def _market_report(settings: Settings, *, dry_run: bool, force: bool) -> int:
    from mister_assistant.jobs.market_report import build_market_report, save_recommendations
    from mister_assistant.store import repo
    from mister_assistant.store.db import connect

    with connect(settings.database_dsn()) as conn:
        report = build_market_report(conn)
        slot = f"mercado-{report.report_date}"
        print(report.message)
        if dry_run:
            return EXIT_OK
        if repo.daily_report_sent(conn, slot) and not force:
            print("Ya se envió el informe de mercado de hoy (usa --force para repetir)")
            return EXIT_OK
        with conn.transaction():
            n = save_recommendations(conn, report)
        print(f"{n} recomendaciones guardadas")
        delivered = _send(settings, report.message)
        with conn.transaction():
            repo.log_daily_report(conn, slot, delivered)
        return EXIT_OK if delivered else EXIT_FAILED


def _gameweek_report(settings: Settings, *, auto: bool, dry_run: bool, refresh: bool) -> int:
    from datetime import UTC, datetime

    from mister_assistant.jobs import gameweek_report as gr
    from mister_assistant.jobs.capture_lineups import run_capture_lineups
    from mister_assistant.jobs.capture_odds import run_capture_odds
    from mister_assistant.sources import futbolfantasy as ff
    from mister_assistant.sources.mister import MisterClient, MisterError, SessionExpiredError
    from mister_assistant.sources.odds import OddsClient
    from mister_assistant.store.db import connect

    now = datetime.now(UTC)
    with connect(settings.database_dsn()) as conn:
        gw = gr.next_gameweek(conn, now)
        if gw is None:
            print("No hay ninguna jornada próxima en la BD (¿falta snapshot-daily?)")
            return EXIT_OK if auto else EXIT_FAILED
        slot = gr.due_slot(conn, gw, now) if auto else gr.manual_slot(now)
        if slot is None:
            print(f"J{gw.number}: nada que enviar ahora (faltan {gr.hours_until(gw, now):.1f} h)")
            return EXIT_OK
        try:
            with MisterClient(settings) as mister:
                if refresh:
                    ff_client = ff.make_client()
                    try:
                        lr = run_capture_lineups(conn, mister, ff_client)
                        print(lr.summary())
                    finally:
                        ff_client.close()
                    key = settings.odds_api_key
                    if key is not None and key.get_secret_value().strip():
                        with OddsClient(key.get_secret_value()) as oc:
                            print(run_capture_odds(conn, oc).summary())
                result = gr.build_report(conn, mister, gw, slot=slot, now=datetime.now(UTC))
        except SessionExpiredError as exc:
            _send(settings, f"🔑 No se pudo preparar el informe de la J{gw.number}: {exc}")
            return EXIT_SESSION_EXPIRED
        except (MisterError, ValueError) as exc:
            _send(settings, f"⚠️ No se pudo preparar el informe de la J{gw.number}: {exc}")
            print(f"✗ {exc}", file=sys.stderr)
            return EXIT_FAILED
        assert result.message is not None
        print(result.message)
        if dry_run:
            return EXIT_OK
        delivered = _send(settings, result.message)
        with conn.transaction():
            gr.log_sent(conn, gw, slot, result.run_id, delivered)
        return EXIT_OK if delivered else EXIT_FAILED


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
