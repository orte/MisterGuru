from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from mister_assistant.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fake_jwt(claims: dict[str, Any]) -> str:
    def seg(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.firma"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test sale a la red: el transporte real de httpx queda bloqueado."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Los tests no pueden llamar a la red")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        mister_token=fake_jwt({"iat": 1_790_000_000, "exp": 1_800_000_000}),
        mister_x_auth="initial-x-auth-value",
        mister_phpsessid="phpsessid-value",
        mister_refresh_token="refresh-token-value",
        mister_min_interval_s=2.5,
        mister_jitter_s=0.0,
    )


Handler = Callable[[httpx.Request], httpx.Response]


# -- Postgres para tests de integración ------------------------------------------
# TEST_DATABASE_URL (CI: servicio postgres) o, si no, un Postgres embebido
# (pgserver) en un directorio temporal. Cada test usa una base de datos nueva.

import os  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Iterator  # noqa: E402

import psycopg  # noqa: E402


@pytest.fixture(scope="session")
def pg_admin_uri(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    uri = os.environ.get("TEST_DATABASE_URL")
    if uri:
        yield uri
        return
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
def db(pg_admin_uri: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    from mister_assistant.store.db import connect, migrate

    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_admin_uri, autocommit=True) as admin:
        admin.execute(f'create database "{name}"')
    test_uri = psycopg.conninfo.make_conninfo(pg_admin_uri, dbname=name)
    conn = connect(test_uri)
    try:
        migrate(conn)
        yield conn
    finally:
        conn.close()
        with psycopg.connect(pg_admin_uri, autocommit=True) as admin:
            admin.execute(f'drop database "{name}" with (force)')
