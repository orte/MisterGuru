"""Cliente HTTP prudente para fuentes públicas: espera entre peticiones,
reintentos de red y errores tipados. Solo GET.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping

import httpx

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0 Safari/537.36"
)


class SourceError(Exception):
    """Fallo de una fuente externa (red, estado HTTP o formato)."""


class PoliteClient:
    def __init__(
        self,
        base_url: str,
        *,
        min_interval_s: float,
        jitter_s: float = 1.0,
        timeout_s: float = 30.0,
        retry_delays_s: tuple[float, ...] = (5.0, 20.0),
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={
                "user-agent": BROWSER_UA,
                "accept-language": "es-ES,es;q=0.9",
                **(headers or {}),
            },
            timeout=timeout_s,
            follow_redirects=True,
            transport=transport,
        )
        self._min_interval = min_interval_s
        self._jitter = jitter_s
        self._retry_delays = retry_delays_s
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._last: float | None = None
        self.requests = 0

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: Mapping[str, str] | None = None) -> httpx.Response:
        delays = iter(self._retry_delays)
        while True:
            self._throttle()
            self.requests += 1
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                delay = next(delays, None)
                if delay is None:
                    raise SourceError(f"error de red en {path}: {type(exc).__name__}") from None
                log.warning("error de red en %s; reintento en %.0f s", path, delay)
                self._sleep(delay)
                continue
            finally:
                self._last = self._clock()
            if resp.status_code == 429 or resp.status_code >= 500:
                delay = next(delays, None)
                if delay is not None:
                    log.warning(
                        "%s respondió %s; reintento en %.0f s", path, resp.status_code, delay
                    )
                    self._sleep(delay)
                    continue
            if resp.status_code != 200:
                raise SourceError(f"{path} respondió {resp.status_code}")
            return resp

    def _throttle(self) -> None:
        if self._last is None:
            return
        wait = self._min_interval + self._rng.uniform(0, self._jitter)
        remaining = wait - (self._clock() - self._last)
        if remaining > 0:
            self._sleep(remaining)
