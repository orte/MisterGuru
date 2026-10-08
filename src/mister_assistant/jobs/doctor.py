"""`mister-assistant doctor`: valida credenciales y lista las ligas de la cuenta."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from mister_assistant.config import Settings
from mister_assistant.sources.mister import (
    MisterClient,
    MisterError,
    SessionExpiredError,
    TokenInfo,
    token_info,
)
from mister_assistant.sources.mister_models import Balance, LeagueSettings

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISSING_CREDENTIALS = 2
EXIT_SESSION_EXPIRED = 3


@dataclass
class DoctorReport:
    missing: list[str] = field(default_factory=list)
    refresh_token_present: bool = False
    token: TokenInfo | None = None
    rotated_token: TokenInfo | None = None
    session_ok: bool = False
    session_expired: bool = False
    error: str | None = None
    active_community_id: int | None = None
    community_ids: list[int] = field(default_factory=list)
    league: LeagueSettings | None = None
    league_error: str | None = None
    balance: Balance | None = None
    x_auth_rotated: bool = False
    token_rotated: bool = False

    @property
    def exit_code(self) -> int:
        if self.missing:
            return EXIT_MISSING_CREDENTIALS
        if self.session_expired:
            return EXIT_SESSION_EXPIRED
        if not self.session_ok or self.error:
            return EXIT_ERROR
        return EXIT_OK


def run_doctor(
    settings: Settings, client_factory: Callable[[Settings], MisterClient] = MisterClient
) -> DoctorReport:
    report = DoctorReport(
        missing=settings.missing_secrets(),
        refresh_token_present=bool(
            settings.mister_refresh_token and settings.mister_refresh_token.get_secret_value()
        ),
    )
    if report.missing:
        return report
    assert settings.mister_token is not None
    report.token = token_info(settings.mister_token.get_secret_value())

    try:
        with client_factory(settings) as client:
            feed = client.feed(offset=0, cards_per_page=1)
            report.session_ok = True
            ctx = feed.cfg.context
            report.community_ids = [c.id for c in ctx.communities]
            report.active_community_id = ctx.community.id if ctx.community else None
            try:
                report.league = client.league_settings()
            except MisterError as exc:
                # /ajax/sw/admin puede no estar disponible si no eres admin de la liga.
                report.league_error = str(exc)
            report.balance = client.balance()
            report.x_auth_rotated = client.x_auth_rotated
            report.token_rotated = client.token_rotated
            if report.token_rotated:
                report.rotated_token = client.token_info()
    except SessionExpiredError as exc:
        report.session_ok = False
        report.session_expired = True
        report.error = f"sesión caducada o inválida: {exc}"
    except MisterError as exc:
        report.error = str(exc)
    return report


def format_report(report: DoctorReport, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    lines: list[str] = []
    if report.missing:
        lines.append("✗ Faltan variables en .env: " + ", ".join(report.missing))
        lines.append("  Cópialas de DevTools (ver .env.example). No las pegues en ningún chat.")
        return "\n".join(lines)

    lines.append("✓ Credenciales presentes (MISTER_TOKEN, MISTER_X_AUTH, MISTER_PHPSESSID)")
    lines.append(
        "  refresh-token: " + ("presente" if report.refresh_token_present else "no configurado")
    )
    # El JWT `token` de Mister vive minutos y el servidor lo reemite si se envía la
    # cookie refresh-token; que esté vencido no implica que la sesión lo esté.
    lines.append("  JWT de .env: " + _describe_token(report.token, now))

    if not report.session_ok:
        lines.append(f"✗ No se pudo hablar con Mister: {report.error}")
        return "\n".join(lines)

    lines.append("✓ Sesión válida")
    if report.error:
        lines.append(f"✗ Error tras validar la sesión: {report.error}")
    if report.league is not None:
        lg = report.league
        lines.append(f"✓ Liga activa: {lg.name} (id {lg.id}) · puntuación {lg.provider}")
        lines.append(
            f"  plantilla máx. {lg.team_limit} · en venta máx. {lg.sale_limit} · "
            f"cláusulas {'sí' if lg.clauses else 'no'} · cesiones {'sí' if lg.loans else 'no'} · "
            f"capitán {'sí' if lg.is_captain_enabled else 'no'} · "
            f"saldos rivales {'visibles' if lg.show_balances else 'ocultos'}"
        )
        lines.append(f"  bonificación por punto: {_eur(lg.prizes.points)}")
    else:
        lines.append(
            f"✓ Liga activa: id {report.active_community_id} (sin detalle: {report.league_error})"
        )

    others = [c for c in report.community_ids if c != report.active_community_id]
    lines.append(
        "  otras ligas de la cuenta: " + (", ".join(map(str, others)) if others else "ninguna")
    )
    if report.balance is not None:
        lines.append(
            f"✓ Saldo: {_eur(report.balance.current)} · futuro {_eur(report.balance.future)} · "
            f"puja máx. {_eur(report.balance.max_debt)}"
        )
    lines.append(
        "  renovación por el servidor: "
        f"x-auth {'sí' if report.x_auth_rotated else 'no'}, "
        f"cookie token {'sí' if report.token_rotated else 'no'}"
    )
    if report.rotated_token is not None:
        lines.append("  JWT renovado: " + _describe_token(report.rotated_token, now))
    return "\n".join(lines)


def _describe_token(info: TokenInfo | None, now: datetime) -> str:
    if info is None:
        return "no es un JWT legible (no se puede saber cuándo caduca)"
    if info.expires_at is None:
        return "JWT sin fecha de caducidad (exp)"
    left = info.expires_at - now
    when = info.expires_at.strftime("%Y-%m-%d %H:%M UTC")
    if left.total_seconds() <= 0:
        return f"vencido el {when}"
    minutes = int(left.total_seconds() // 60)
    if minutes < 120:
        return f"caduca el {when} (en {minutes} min)"
    return f"caduca el {when} (en {left.days} d {left.seconds // 3600} h)"


def _eur(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + " €"
