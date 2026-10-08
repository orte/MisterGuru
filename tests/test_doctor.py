from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from conftest import load_fixture

from mister_assistant.config import Settings
from mister_assistant.jobs.doctor import (
    EXIT_MISSING_CREDENTIALS,
    EXIT_OK,
    EXIT_SESSION_EXPIRED,
    format_report,
    run_doctor,
)
from mister_assistant.sources.mister import MisterClient

ROUTE_FIXTURES = {
    "/ajax/feed": "feed.json",
    "/ajax/sw/admin": "admin.json",
    "/ajax/balance": "balance.json",
}


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=load_fixture(ROUTE_FIXTURES[request.url.path]))


def test_doctor_reports_league(settings: Settings) -> None:
    report = run_doctor(
        settings,
        lambda s: MisterClient(s, transport=httpx.MockTransport(ok_handler), sleep=lambda _: None),
    )
    assert report.exit_code == EXIT_OK
    assert report.active_community_id == 111
    assert report.league is not None and report.league.name == "Liga de prueba"

    text = format_report(report, now=datetime(2026, 10, 8, tzinfo=UTC))
    assert "Liga activa: Liga de prueba (id 111)" in text
    assert "otras ligas de la cuenta: 222" in text
    assert "4.299.770 €" in text
    assert "caduca el 2027-01-15" in text
    # Nunca se imprime ningún secreto.
    for secret in settings.secret_values():
        assert secret not in text
    assert "rotated-x-auth-value" not in text


def test_doctor_falls_back_when_admin_unavailable(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ajax/sw/admin":
            return httpx.Response(200, json={"status": "error", "data": "no admin"})
        return ok_handler(request)

    report = run_doctor(
        settings,
        lambda s: MisterClient(s, transport=httpx.MockTransport(handler), sleep=lambda _: None),
    )
    assert report.exit_code == EXIT_OK
    assert "Liga activa: id 111" in format_report(report)


def test_doctor_session_expired(settings: Settings) -> None:
    report = run_doctor(
        settings,
        lambda s: MisterClient(
            s, transport=httpx.MockTransport(lambda r: httpx.Response(401)), sleep=lambda _: None
        ),
    )
    assert report.exit_code == EXIT_SESSION_EXPIRED
    assert "sesión caducada" in format_report(report)


def test_doctor_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MISTER_TOKEN", "MISTER_X_AUTH", "MISTER_PHPSESSID"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)
    report = run_doctor(s)
    assert report.exit_code == EXIT_MISSING_CREDENTIALS
    assert "MISTER_TOKEN" in format_report(report)
