"""Parsers de las páginas HTML parciales de Mister (/standings, /market).

Aislados aquí para que un cambio de maquetación rompa solo este módulo, y con
un error tipado (`MisterParseError`) que el job registra sin caerse entero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from bs4 import BeautifulSoup, Tag

from mister_assistant.sources.mister import MisterError


class MisterParseError(MisterError):
    """El HTML no tiene la forma esperada."""


@dataclass(frozen=True)
class StandingRow:
    manager_id: int
    slug: str
    name: str
    rank: int
    points: int
    squad_size: int | None
    team_value: int | None
    is_me: bool


@dataclass(frozen=True)
class MarketRow:
    player_id: int
    slug: str
    short_name: str
    position: int
    team_id: int | None
    points: int | None
    market_value: int | None
    value_trend: int  # 1 sube, -1 baja, 0 sin flecha
    avg_points: float | None
    sale_price: int
    seller_manager_id: int | None  # None = lo vende el juego
    sale_ends_at: datetime | None


_USER_HREF = re.compile(r"users/(\d+)/([^/?#]+)")
_PLAYER_HREF = re.compile(r"players/(\d+)/([^/?#]+)")
_TEAM_LOGO = re.compile(r"/teams/(\d+)\.png")
_PLAYED = re.compile(r"(\d+)\s+jugadores\s+·\s+€\s*([\d.]+)")


def parse_standings(html: str) -> list[StandingRow]:
    """Clasificación general (panel `panel-total`)."""
    soup = BeautifulSoup(html, "html.parser")
    panel = soup.select_one(".panel-total")
    if panel is None:
        raise MisterParseError("standings: falta .panel-total")
    rows: list[StandingRow] = []
    for link in panel.select("a.user"):
        match = _USER_HREF.search(str(link.get("href", "")))
        if match is None:
            raise MisterParseError("standings: enlace de usuario sin id")
        name_el = link.select_one(".name")
        played = _PLAYED.search(_text(link.select_one(".played")))
        rows.append(
            StandingRow(
                manager_id=int(match.group(1)),
                slug=match.group(2),
                name=_text(name_el),
                rank=_int(_text(link.select_one(".position"))),
                points=_int(_own_text(link.select_one(".points"))),
                squad_size=int(played.group(1)) if played else None,
                team_value=_money(played.group(2)) if played else None,
                is_me=name_el is not None and "myself" in (name_el.get("class") or []),
            )
        )
    if not rows:
        raise MisterParseError("standings: clasificación vacía")
    return rows


def parse_market(html: str) -> list[MarketRow]:
    """Jugadores en venta en el mercado del día (`#list-on-sale`)."""
    soup = BeautifulSoup(html, "html.parser")
    lst = soup.select_one("#list-on-sale")
    if lst is None:
        raise MisterParseError("market: falta #list-on-sale")
    rows: list[MarketRow] = []
    for li in lst.find_all("li", recursive=False):
        if not isinstance(li, Tag):
            continue
        link = li.select_one("a.player")
        match = _PLAYER_HREF.search(str(link.get("href", ""))) if link else None
        if link is None or match is None:
            raise MisterParseError("market: fila sin enlace de jugador")
        logos = [_TEAM_LOGO.search(str(img.get("src", ""))) for img in link.select("img.team-logo")]
        owner = str(li.get("data-owner", "")).strip()
        ends = str(li.get("data-ends", "")).strip()
        under = link.select_one(".underName")
        arrow = link.select_one(".value-arrow")
        arrow_cls: list[str] = list(arrow.get("class") or []) if arrow else []
        avg = _text(link.select_one(".avg")).replace(",", ".")
        rows.append(
            MarketRow(
                player_id=int(match.group(1)),
                slug=match.group(2),
                short_name=_text(link.select_one(".name")),
                position=_int(str(li.get("data-position", ""))),
                team_id=int(logos[0].group(1)) if logos and logos[0] else None,
                points=_opt_int(_text(link.select_one(".icons .points"))),
                market_value=_money(_text(under)) if under else None,
                value_trend=1 if "green" in arrow_cls else -1 if "red" in arrow_cls else 0,
                avg_points=float(avg) if re.fullmatch(r"-?\d+(\.\d+)?", avg) else None,
                sale_price=_int(str(li.get("data-price", ""))),
                seller_manager_id=int(owner) if owner.isdigit() and owner != "0" else None,
                sale_ends_at=datetime.fromtimestamp(int(ends), UTC) if ends.isdigit() else None,
            )
        )
    return rows


def _text(el: Tag | None) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el is not None else ""


def _own_text(el: Tag | None) -> str:
    """Texto directo del nodo, sin el de sus hijos (p. ej. «292» sin «Pts»)."""
    if el is None:
        return ""
    return " ".join(s.strip() for s in el.find_all(string=True, recursive=False)).strip()


def _int(text: str) -> int:
    value = _opt_int(text)
    if value is None:
        raise MisterParseError(f"se esperaba un número y llegó {text[:20]!r}")
    return value


def _opt_int(text: str) -> int | None:
    digits = text.replace(".", "").strip()
    return int(digits) if re.fullmatch(r"-?\d+", digits) else None


def _money(text: str) -> int | None:
    m = re.search(r"\d[\d.]*", text)
    return int(m.group(0).replace(".", "")) if m else None
