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
