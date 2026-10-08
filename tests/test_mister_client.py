from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import Handler, fake_jwt, load_fixture

from mister_assistant.config import Settings
from mister_assistant.sources import mister
from mister_assistant.sources.mister import (
    ROUTES,
    ForbiddenRouteError,
    MisterApiError,
    MisterClient,
    Route,
    SessionExpiredError,
    token_info,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_client(
    settings: Settings, handler: Handler, clock: FakeClock | None = None
) -> MisterClient:
    clock = clock or FakeClock()
    return MisterClient(
        settings, transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock
    )


def json_response(body: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


# -- lista blanca -------------------------------------------------------------


def test_unknown_route_is_rejected_without_network(settings: Settings) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return json_response({})

    client = make_client(settings, handler)
    with pytest.raises(ForbiddenRouteError):
        client.call("bid")
    assert calls == []


def test_unknown_param_is_rejected(settings: Settings) -> None:
    client = make_client(settings, lambda r: json_response({"status": "ok"}))
    with pytest.raises(ForbiddenRouteError, match="amount"):
        client.call("user", id=1, slug="x", amount=1000)


def test_route_outside_whitelist_is_rejected_even_if_built_by_hand(settings: Settings) -> None:
    client = make_client(settings, lambda r: json_response({"status": "ok"}))
    with pytest.raises(ForbiddenRouteError):
        client._request("x", Route("POST", "/ajax/sw/market-bid", "json"), {})


def test_whitelist_has_no_write_looking_routes() -> None:
    forbidden = (
        "bid",
        "offer",
        "sell",
        "sale",
        "buy",
        "lineup",
        "clause-",
        "save",
        "change",
        "delete",
    )
    for route in ROUTES.values():
        assert not any(word in route.path for word in forbidden), route.path


def test_whitelist_matches_catalog() -> None:
    catalog = (mister.__file__.rsplit("/src/", 1)[0]) + "/docs/mister-endpoints.md"
    with open(catalog, encoding="utf-8") as fh:
        text = fh.read()
    for route in ROUTES.values():
        assert f"`{route.path}`" in text, f"{route.path} no está documentada en el catálogo"


# -- forma de las peticiones --------------------------------------------------


def test_sends_auth_headers_cookies_and_fixed_params(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response({"status": "ok", "data": {}})

    client = make_client(settings, handler)
    client.user(15448221, "tixidor")

    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/ajax/sw/users"
    assert req.headers["x-auth"] == "initial-x-auth-value"
    assert req.headers["x-requested-with"] == "XMLHttpRequest"
    cookie = req.headers["cookie"]
    for name in ("token=", "PHPSESSID=phpsessid-value", "refresh-token=refresh-token-value"):
        assert name in cookie
    form = parse_qs(req.content.decode())
    assert form == {"post": ["users"], "id": ["15448221"], "slug": ["tixidor"], "comments": ["0"]}


def test_html_pages_use_partial_request(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text='<div id="partial-wrapper"><div id="partial-content">')

    resp = make_client(settings, handler).page("market")
    assert seen[0].headers["partial-request"] == "true"
    assert seen[0].url.path == "/market"
    assert resp.payload is None and "partial-content" in resp.text


# -- espera entre peticiones --------------------------------------------------


def test_throttles_between_requests(settings: Settings) -> None:
    clock = FakeClock()
    client = make_client(settings, lambda r: json_response(load_fixture("balance.json")), clock)
    client.balance()
    assert clock.slept == []
    clock.now += 1.0
    client.balance()
    assert clock.slept == [pytest.approx(1.5)]


# -- reintentos de red ---------------------------------------------------------


def test_retries_transient_network_errors(settings: Settings) -> None:
    clock = FakeClock()
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.RemoteProtocolError("corte", request=request)
        return json_response(load_fixture("balance.json"))

    client = make_client(settings, flaky, clock)
    assert client.balance().current == 4_299_770
    assert attempts["n"] == 3 and client.requests == 3
    # Esperas de reintento (más la espera normal entre peticiones).
    assert 5.0 in clock.slept and 20.0 in clock.slept


def test_gives_up_after_retries(settings: Settings) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=request)

    client = make_client(settings, down)
    with pytest.raises(MisterApiError, match="Error de red"):
        client.balance()
    assert client.requests == 3


def test_http_errors_are_not_retried(settings: Settings) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(MisterApiError):
        make_client(settings, handler).balance()
    assert len(calls) == 1


# -- sesión -------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_status_means_session_expired(settings: Settings, status: int) -> None:
    client = make_client(settings, lambda r: httpx.Response(status))
    with pytest.raises(SessionExpiredError):
        client.balance()


def test_redirect_to_login_means_session_expired(settings: Settings) -> None:
    client = make_client(
        settings, lambda r: httpx.Response(302, headers={"location": "/new-onboarding/auth"})
    )
    with pytest.raises(SessionExpiredError):
        client.balance()


def test_login_html_instead_of_json_means_session_expired(settings: Settings) -> None:
    html = '<form><input type="password" name="password"></form>'
    client = make_client(settings, lambda r: httpx.Response(200, text=html))
    with pytest.raises(SessionExpiredError):
        client.balance()


def test_error_status_is_api_error(settings: Settings) -> None:
    client = make_client(settings, lambda r: json_response({"status": "error", "data": "boom"}))
    with pytest.raises(MisterApiError):
        client.balance()


def test_x_auth_rotates_from_cfg_auth(settings: Settings) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-auth"])
        if request.url.path == "/ajax/feed":
            return json_response(load_fixture("feed.json"))
        return json_response(load_fixture("balance.json"))

    client = make_client(settings, handler)
    page = client.feed()
    client.balance()
    assert [c.id for c in page.cfg.context.communities] == [222, 111]
    assert seen == ["initial-x-auth-value", "rotated-x-auth-value"]
    assert client.x_auth_rotated


def test_detects_token_cookie_rotation(settings: Settings) -> None:
    new_token = fake_jwt({"exp": 1_900_000_000})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(load_fixture("balance.json")).encode(),
            headers={
                "content-type": "application/json",
                "set-cookie": f"token={new_token}; Path=/; Domain=mister.mundodeportivo.com",
            },
        )

    client = make_client(settings, handler)
    assert not client.token_rotated
    client.balance()
    assert client.token_rotated
    info = client.token_info()
    assert info is not None and info.expires_at is not None
    assert info.expires_at.year == 2030


def test_missing_credentials_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISTER_X_AUTH", raising=False)
    monkeypatch.delenv("MISTER_PHPSESSID", raising=False)
    s = Settings(_env_file=None, mister_token="t")
    with pytest.raises(SessionExpiredError, match="MISTER_X_AUTH"):
        MisterClient(s)


def test_token_info_handles_non_jwt() -> None:
    assert token_info("no-es-un-jwt") is None
    info = token_info(fake_jwt({"iat": 0, "exp": 60}))
    assert info is not None and info.expires_at is not None
    assert info.expires_at.timestamp() == 60
    # Formato real de Mister: exp como cadena numérica.
    info = token_info(fake_jwt({"alg": "HS256", "exp": "120", "userid": "1"}))
    assert info is not None and info.expires_at is not None
    assert info.expires_at.timestamp() == 120


# -- modelos ------------------------------------------------------------------


def test_parses_league_settings_and_balance(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        name = "admin.json" if request.url.path == "/ajax/sw/admin" else "balance.json"
        return json_response(load_fixture(name))

    client = make_client(settings, handler)
    league = client.league_settings()
    assert league.name == "Liga de prueba"
    assert league.provider == "mix"
    assert league.prizes.points == 100_000
    assert league.prizes.positions[2] == 400_000
    assert client.balance().max_debt == 20_699_770
