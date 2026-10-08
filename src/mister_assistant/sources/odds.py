"""The Odds API (the-odds-api.com): 1X2 y más/menos goles de LaLiga.

Coste por consulta = mercados por regiones (aquí 2 por 1 = 2 créditos); el plan
gratuito da 500 al mes. Las cuotas de goleador no existen para fútbol en esta
API, así que P(gol) sale del xG (Fase 3). Sin `ODDS_API_KEY` no se consulta.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import mean
from typing import Any

import httpx

from mister_assistant.sources.http import PoliteClient, SourceError

log = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com"
SPORT = "soccer_spain_la_liga"
SOURCE = "the_odds_api"
MARKETS = ("h2h", "totals")
REGIONS = "eu"


@dataclass(frozen=True)
class OddsQuota:
    remaining: int | None
    used: int | None
    last_cost: int | None


@dataclass(frozen=True)
class OddsLine:
    market: str  # h2h | totals
    outcome: str  # home | draw | away | over | under
    point: float  # línea de goles en totals; 0 en h2h
    price_avg: float
    prob_fair: float  # probabilidad implícita sin margen, media entre casas
    bookmakers: int


@dataclass(frozen=True)
class OddsEvent:
    event_id: str
    commence_at: datetime
    home_name: str
    away_name: str
    lines: list[OddsLine]


class OddsClient:
    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._key = api_key
        self._http = PoliteClient(BASE_URL, min_interval_s=1.0, jitter_s=0.5, transport=transport)
        self.quota = OddsQuota(None, None, None)

    @property
    def requests(self) -> int:
        return self._http.requests

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OddsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def odds(self) -> list[dict[str, Any]]:
        resp = self._http.get(
            f"/v4/sports/{SPORT}/odds",
            params={
                "apiKey": self._key,
                "regions": REGIONS,
                "markets": ",".join(MARKETS),
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )
        self.quota = OddsQuota(
            remaining=_header_int(resp, "x-requests-remaining"),
            used=_header_int(resp, "x-requests-used"),
            last_cost=_header_int(resp, "x-requests-last"),
        )
        data = resp.json()
        if not isinstance(data, list):
            raise SourceError("odds: respuesta inesperada")
        return data


def _header_int(resp: httpx.Response, name: str) -> int | None:
    value = resp.headers.get(name, "")
    try:
        return int(float(value))
    except ValueError:
        return None


def consensus(events: list[dict[str, Any]]) -> list[OddsEvent]:
    """Probabilidad sin margen por casa (normalizada a 1 en cada mercado) y media."""
    out: list[OddsEvent] = []
    for ev in events:
        home, away = str(ev["home_team"]), str(ev["away_team"])
        fair: dict[tuple[str, str, float], list[float]] = defaultdict(list)
        prices: dict[tuple[str, str, float], list[float]] = defaultdict(list)
        for book in ev.get("bookmakers") or []:
            for market in book.get("markets") or []:
                key = str(market.get("key"))
                if key not in MARKETS:
                    continue
                groups: dict[float, list[tuple[str, float]]] = defaultdict(list)
                for o in market.get("outcomes") or []:
                    price = float(o["price"])
                    if price <= 1.0:
                        continue
                    outcome = _outcome(key, str(o["name"]), home, away)
                    point = float(o.get("point") or 0.0) if key == "totals" else 0.0
                    groups[point].append((outcome, price))
                for point, outs in groups.items():
                    expected = 3 if key == "h2h" else 2
                    if len(outs) != expected:
                        continue
                    total = sum(1 / p for _, p in outs)
                    for outcome, price in outs:
                        fair[(key, outcome, point)].append((1 / price) / total)
                        prices[(key, outcome, point)].append(price)
        lines = [
            OddsLine(
                market=k[0],
                outcome=k[1],
                point=k[2],
                price_avg=round(mean(prices[k]), 3),
                prob_fair=round(mean(v), 4),
                bookmakers=len(v),
            )
            for k, v in sorted(fair.items())
        ]
        out.append(
            OddsEvent(
                event_id=str(ev["id"]),
                commence_at=datetime.fromisoformat(str(ev["commence_time"]).replace("Z", "+00:00")),
                home_name=home,
                away_name=away,
                lines=lines,
            )
        )
    return out


def _outcome(market: str, name: str, home: str, away: str) -> str:
    if market == "totals":
        return name.strip().lower()  # over | under
    if name == home:
        return "home"
    if name == away:
        return "away"
    if name.lower() == "draw":
        return "draw"
    raise SourceError(f"odds: resultado desconocido {name!r}")
