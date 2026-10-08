"""Cliente de **solo lectura** para Mister (mister.mundodeportivo.com).

Mister no tiene API pública: la web llama a rutas `/ajax/*` (JSON) y pide
páginas parciales (HTML) con la cabecera `partial-request: true`. Casi todas son
POST aunque solo consulten, así que la seguridad no puede basarse en el método:
el cliente tiene una **lista blanca** de rutas de consulta con los parámetros
permitidos en cada una. Cualquier otra ruta o parámetro lanza `ForbiddenRouteError`
antes de tocar la red. No añadir rutas de escritura aquí (ver PLAN.md §7).

Catálogo de rutas: docs/mister-endpoints.md.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from mister_assistant.config import ConfigError, Settings
from mister_assistant.sources.mister_models import (
    Balance,
    FeedPage,
    LeagueSettings,
)

log = logging.getLogger(__name__)

ResponseKind = Literal["json", "html"]

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)


class MisterError(Exception):
    """Error base del cliente de Mister."""


class ForbiddenRouteError(MisterError):
    """Ruta o parámetro fuera de la lista blanca de solo lectura."""


class SessionExpiredError(MisterError):
    """La sesión no es válida: hay que renovar token/x-auth en `.env`."""


class MisterApiError(MisterError):
    """Respuesta inesperada de Mister (estado HTTP, formato o `status != ok`)."""


@dataclass(frozen=True)
class Route:
    method: Literal["GET", "POST"]
    path: str
    kind: ResponseKind
    params: frozenset[str] = frozenset()
    # Parámetros que la web siempre envía con un valor fijo (p. ej. post=users).
    fixed: Mapping[str, str] = field(default_factory=dict)


# Lista blanca. Clave = nombre lógico usado por el resto del código.
ROUTES: Mapping[str, Route] = {
    "feed": Route(
        "POST", "/ajax/feed", "json", frozenset({"offset", "cardsPerPage", "end", "loading"})
    ),
    "balance": Route("POST", "/ajax/balance", "json"),
    "user": Route(
        "POST", "/ajax/sw/users", "json", frozenset({"id", "slug", "comments"}), {"post": "users"}
    ),
    "player": Route(
        "POST",
        "/ajax/sw/players",
        "json",
        frozenset({"id", "slug", "comments"}),
        {"post": "players"},
    ),
    "gameweek": Route(
        "POST", "/ajax/sw/gameweek", "json", frozenset({"id", "comments"}), {"post": "gameweek"}
    ),
    "player_gameweek": Route(
        "POST",
        "/ajax/player-gameweek",
        "json",
        frozenset({"id_manager", "id_gameweek", "id_player"}),
    ),
    "league_settings": Route("POST", "/ajax/sw/admin", "json", fixed={"post": "admin"}),
    "market_page": Route("POST", "/market", "html"),
    "team_page": Route("POST", "/team", "html"),
    "standings_page": Route("POST", "/standings", "html"),
    "feed_page": Route("POST", "/feed", "html"),
}

_ALLOWED: frozenset[tuple[str, str]] = frozenset((r.method, r.path) for r in ROUTES.values())

_LOGIN_HINTS = ("login", "signin", "sign-in", "onboarding", "auth")

# Esperas antes de reintentar un error de red transitorio (corte, timeout).
RETRY_DELAYS_S: tuple[float, ...] = (5.0, 20.0)


@dataclass(frozen=True)
class MisterResponse:
    """Respuesta cruda + parseada. Lo crudo se guardará en `raw_responses` (Fase 1)."""

    route: str
    path: str
    params: Mapping[str, str]
    status_code: int
    fetched_at: datetime
    text: str
    payload: Any = None  # JSON completo para rutas json; None para html

    @property
    def data(self) -> Any:
        if isinstance(self.payload, dict):
            return self.payload.get("data")
        return None


@dataclass(frozen=True)
class TokenInfo:
    issued_at: datetime | None
    expires_at: datetime | None


def decode_jwt_claims(token: str) -> dict[str, Any] | None:
    """Lee (sin verificar) el payload de un JWT. Devuelve None si no lo es."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment))
    except (binascii.Error, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


def token_info(token: str) -> TokenInfo | None:
    claims = decode_jwt_claims(token)
    if claims is None:
        return None

    def ts(key: str) -> datetime | None:
        value = claims.get(key)
        # Mister emite `exp` como cadena numérica ("1791…"), no como número.
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        return datetime.fromtimestamp(value, UTC) if isinstance(value, int | float) else None

    return TokenInfo(issued_at=ts("iat"), expires_at=ts("exp"))


class MisterClient:
    """Cliente httpx de solo lectura, con espera entre peticiones."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        missing = settings.missing_secrets()
        if missing:
            raise SessionExpiredError(f"Faltan credenciales en el entorno: {', '.join(missing)}")
        invalid = settings.invalid_header_secrets()
        if invalid:
            raise ConfigError(
                f"{', '.join(invalid)} tiene caracteres que no valen en una cabecera HTTP"
                " (espacios, saltos de línea o comillas en medio). Vuelve a copiar el valor."
            )
        assert settings.mister_token and settings.mister_x_auth and settings.mister_phpsessid

        self._settings = settings
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._last_request_at: float | None = None

        self._initial_token = settings.mister_token.get_secret_value()
        self._token = self._initial_token
        self._x_auth = settings.mister_x_auth.get_secret_value()
        self.x_auth_rotated = False
        self.requests = 0

        base = settings.mister_base_url.rstrip("/")
        host = httpx.URL(base).host
        self._host = host
        cookies = httpx.Cookies()
        cookies.set("token", self._initial_token, domain=host)
        cookies.set("PHPSESSID", settings.mister_phpsessid.get_secret_value(), domain=host)
        cookies.set("authenticated", "true", domain=host)
        if settings.mister_refresh_token and settings.mister_refresh_token.get_secret_value():
            cookies.set(
                "refresh-token", settings.mister_refresh_token.get_secret_value(), domain=host
            )

        self._http = httpx.Client(
            base_url=base,
            cookies=cookies,
            headers={
                "user-agent": _BROWSER_UA,
                "accept": "*/*",
                "accept-language": "es-ES,es;q=0.9,en;q=0.8",
                "origin": base,
                "referer": f"{base}/feed",
                "x-requested-with": "XMLHttpRequest",
            },
            timeout=settings.mister_timeout_s,
            follow_redirects=False,
            transport=transport,
        )

    # -- ciclo de vida -------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MisterClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- estado de la sesión -------------------------------------------------

    @property
    def token_rotated(self) -> bool:
        """True si el servidor ha enviado una cookie `token` nueva (Set-Cookie)."""
        return self._token != self._initial_token

    def token_info(self) -> TokenInfo | None:
        return token_info(self._token)

    def _track_token_cookie(self, resp: httpx.Response) -> None:
        new = resp.cookies.get("token")
        if new and new != self._token:
            # Sustituye la cookie (el Set-Cookie puede llegar con otro dominio y duplicarla).
            self._http.cookies.delete("token")
            self._http.cookies.set("token", new, domain=self._host)
            self._token = new
            log.info("el servidor ha renovado la cookie token (solo en memoria)")

    # -- petición genérica ---------------------------------------------------

    def call(self, route_name: str, **params: str | int | bool) -> MisterResponse:
        route = ROUTES.get(route_name)
        if route is None:
            raise ForbiddenRouteError(f"Ruta no permitida: {route_name!r}")
        unknown = set(params) - route.params
        if unknown:
            raise ForbiddenRouteError(
                f"Parámetros no permitidos para {route_name}: {', '.join(sorted(unknown))}"
            )
        form = {**route.fixed, **{k: _form_value(v) for k, v in params.items()}}
        return self._request(route_name, route, form)

    def _request(self, route_name: str, route: Route, form: dict[str, str]) -> MisterResponse:
        # Segunda barrera: aunque alguien construya un Route a mano, solo salen
        # peticiones a pares (método, ruta) de la lista blanca.
        if (route.method, route.path) not in _ALLOWED:
            raise ForbiddenRouteError(f"{route.method} {route.path} no está en la lista blanca")

        resp = self._send(route, form)

        self._track_token_cookie(resp)
        self._check_status(route, resp)
        payload: Any = None
        if route.kind == "json":
            payload = self._parse_json(route, resp)
            self._maybe_rotate_x_auth(payload)
        else:
            self._check_html(route, resp)

        return MisterResponse(
            route=route_name,
            path=route.path,
            params=form,
            status_code=resp.status_code,
            fetched_at=datetime.now(UTC),
            text=resp.text,
            payload=payload,
        )

    def _send(self, route: Route, form: dict[str, str]) -> httpx.Response:
        """Envía la petición; reintenta los errores de red transitorios.

        Solo se reintenta lo que no llegó a responder (transporte): un 4xx/5xx o
        una sesión caducada se tratan después, sin reintentos.
        """
        delays = iter(RETRY_DELAYS_S)
        while True:
            self._throttle()
            self.requests += 1
            headers = {"x-auth": self._x_auth}
            if route.kind == "html":
                headers["partial-request"] = "true"
            log.info("mister %s %s", route.method, route.path)
            try:
                return self._http.request(
                    route.method,
                    route.path,
                    data=form if route.method == "POST" else None,
                    params=form if route.method == "GET" else None,
                    headers=headers,
                )
            except httpx.LocalProtocolError as exc:
                # Error al construir la petición (p. ej. una cabecera inválida): reintentar
                # no lo arregla.
                raise MisterApiError(
                    f"Petición inválida a {route.path}: {type(exc).__name__}"
                ) from exc
            except httpx.TransportError as exc:
                delay = next(delays, None)
                if delay is None:
                    raise MisterApiError(
                        f"Error de red en {route.path}: {type(exc).__name__}"
                    ) from exc
                log.warning(
                    "error de red en %s (%s); reintento en %.0f s",
                    route.path,
                    type(exc).__name__,
                    delay,
                )
                self._sleep(delay)
            except httpx.HTTPError as exc:
                raise MisterApiError(f"Error en {route.path}: {type(exc).__name__}") from exc
            finally:
                self._last_request_at = self._clock()

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        wait = self._settings.mister_min_interval_s + self._rng.uniform(
            0, self._settings.mister_jitter_s
        )
        remaining = wait - (self._clock() - self._last_request_at)
        if remaining > 0:
            self._sleep(remaining)

    @staticmethod
    def _check_status(route: Route, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403, 419, 440):
            raise SessionExpiredError(f"{route.path} respondió {resp.status_code}: sesión caducada")
        if resp.is_redirect:
            location = resp.headers.get("location", "").lower()
            if any(hint in location for hint in _LOGIN_HINTS) or location.rstrip("/") in ("", "/"):
                raise SessionExpiredError(f"{route.path} redirige a login: sesión caducada")
            raise MisterApiError(f"{route.path} redirige inesperadamente ({resp.status_code})")
        if resp.status_code != 200:
            raise MisterApiError(f"{route.path} respondió {resp.status_code}")

    @staticmethod
    def _parse_json(route: Route, resp: httpx.Response) -> Any:
        try:
            payload = resp.json()
        except ValueError as exc:
            if _looks_like_login(resp.text):
                raise SessionExpiredError(f"{route.path} devolvió la página de login") from exc
            raise MisterApiError(f"{route.path} no devolvió JSON") from exc
        if not isinstance(payload, dict):
            raise MisterApiError(f"{route.path}: JSON inesperado ({type(payload).__name__})")
        status = payload.get("status")
        if status != "ok":
            detail = json.dumps(
                payload.get("data") or payload.get("message") or "", ensure_ascii=False
            )
            if any(hint in detail.lower() for hint in ("sesi", "login", "auth", "token")):
                raise SessionExpiredError(f"{route.path}: status={status!r} (sesión)")
            raise MisterApiError(f"{route.path}: status={status!r} {detail[:200]}")
        return payload

    @staticmethod
    def _check_html(route: Route, resp: httpx.Response) -> None:
        if "partial-content" not in resp.text:
            if _looks_like_login(resp.text):
                raise SessionExpiredError(f"{route.path} devolvió la página de login")
            raise MisterApiError(f"{route.path}: HTML parcial sin #partial-content")

    def _maybe_rotate_x_auth(self, payload: dict[str, Any]) -> None:
        # Algunas respuestas (p. ej. /ajax/feed) traen `cfg.auth`: el x-auth vigente.
        cfg = payload.get("cfg")
        new = cfg.get("auth") if isinstance(cfg, dict) else None
        if isinstance(new, str) and new and new != self._x_auth:
            self._x_auth = new
            self.x_auth_rotated = True
            log.info("x-auth actualizado desde cfg.auth (solo en memoria)")

    # -- consultas tipadas ---------------------------------------------------

    def balance(self) -> Balance:
        return Balance.model_validate(self.call("balance").data)

    def feed(self, offset: int = 0, cards_per_page: int = 20) -> FeedPage:
        resp = self.call(
            "feed", offset=offset, cardsPerPage=cards_per_page, end=False, loading=True
        )
        return FeedPage.model_validate(resp.payload)

    def league_settings(self) -> LeagueSettings:
        data = self.call("league_settings").data
        if not isinstance(data, dict):
            raise MisterApiError("/ajax/sw/admin: falta data")
        return LeagueSettings.model_validate(data.get("community"))

    def user(self, user_id: int, slug: str) -> MisterResponse:
        return self.call("user", id=user_id, slug=slug, comments=0)

    def player(self, player_id: int, slug: str) -> MisterResponse:
        return self.call("player", id=player_id, slug=slug, comments=0)

    def gameweek(self, gameweek_id: int | None = None) -> MisterResponse:
        """Jornada por id; sin id, la jornada actual (o la próxima si no ha empezado)."""
        if gameweek_id is None:
            return self.call("gameweek", comments=0)
        return self.call("gameweek", id=gameweek_id, comments=0)

    def player_gameweek(self, manager_id: int, gameweek_id: int, player_id: int) -> MisterResponse:
        return self.call(
            "player_gameweek", id_manager=manager_id, id_gameweek=gameweek_id, id_player=player_id
        )

    def page(self, name: Literal["market", "team", "standings", "feed"]) -> MisterResponse:
        return self.call(f"{name}_page")


def _form_value(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _looks_like_login(text: str) -> bool:
    head = text[:5000].lower()
    return 'type="password"' in head or "iniciar sesión" in head or "/login" in head
