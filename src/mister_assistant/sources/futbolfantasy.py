"""Fútbol Fantasy (futbolfantasy.com): alineaciones probables y estado físico.

robots.txt permite el rastreo. Aun así: pocas peticiones (índice + 10 partidos
por captura), espaciadas 5 s, y solo varias veces por semana. Las fichas de
partido pesan ~2 MB: como crudo se guardan solo los atributos de cada jugador
(`raw_players`, ~60 KB).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from mister_assistant.sources.http import PoliteClient, SourceError

BASE_URL = "https://www.futbolfantasy.com"
INDEX_PATH = "/laliga/posibles-alineaciones"
SOURCE = "futbolfantasy"


class FutbolFantasyParseError(SourceError):
    """El HTML de Fútbol Fantasy no tiene la forma esperada."""


@dataclass(frozen=True)
class FFMatch:
    match_id: str  # id de Fútbol Fantasy (p. ej. "22499")
    path: str  # /partidos/22499-betis-osasuna
    home_name: str
    away_name: str
    home_team_id: str  # id del escudo en Fútbol Fantasy
    away_team_id: str
    jornada: int | None


@dataclass(frozen=True)
class FFPlayer:
    player_id: str  # id de Fútbol Fantasy (clase jugador_<id>)
    name: str
    slug: str | None
    side: str  # "local" | "visitante"
    role: str  # "titular" | "suplente"
    probability: float | None  # 0-1
    injury_code: int | None  # -1 = sin problema; otros códigos = lesión/duda
    goalkeeper: bool


def make_client(**kwargs: object) -> PoliteClient:
    return PoliteClient(BASE_URL, min_interval_s=5.0, jitter_s=2.0, **kwargs)  # type: ignore[arg-type]


_MATCH_HREF = re.compile(r"/partidos/(\d+)-[^\"'#?]+")
_ESCUDO = re.compile(r"/escudo\w*/(\d+)\.png")
_JORNADA = re.compile(r"Jornada\s+(\d+)")
_PLAYER_CLASS = re.compile(r"^jugador_(\d+)$")
_PLAYER_SLUG = re.compile(r"/jugadores/([^/]+)/")


def parse_index(html: str) -> list[FFMatch]:
    """Partidos de la jornada actual (bloque `section.proxjornada` principal)."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text() if soup.title else ""
    m = _JORNADA.search(title)
    jornada = int(m.group(1)) if m else None
    section = soup.select_one("section.proxjornada.mw-50-desktop") or soup.select_one(
        "section.proxjornada"
    )
    if section is None:
        raise FutbolFantasyParseError("índice: falta section.proxjornada")
    matches: list[FFMatch] = []
    for a in section.select("a.partido[href]"):
        href = str(a["href"])
        mm = _MATCH_HREF.search(href)
        home = a.select_one("img.escudo.local")
        away = a.select_one("img.escudo.visitante")
        if mm is None or home is None or away is None:
            raise FutbolFantasyParseError(f"índice: partido sin equipos ({href[-40:]})")
        matches.append(
            FFMatch(
                match_id=mm.group(1),
                path=mm.group(0),
                home_name=str(home.get("alt", "")).strip(),
                away_name=str(away.get("alt", "")).strip(),
                home_team_id=_escudo_id(home),
                away_team_id=_escudo_id(away),
                jornada=jornada,
            )
        )
    if not matches:
        raise FutbolFantasyParseError("índice: sin partidos")
    return matches


def _escudo_id(img: Tag) -> str:
    src = str(img.get("data-src") or img.get("src") or "")
    m = _ESCUDO.search(src)
    if m is None:
        raise FutbolFantasyParseError("índice: escudo sin id")
    return m.group(1)


def parse_match(html: str) -> list[FFPlayer]:
    """Once probable y suplentes de ambos equipos, con probabilidad y lesión."""
    soup = BeautifulSoup(html, "html.parser")
    players: list[FFPlayer] = []
    seen: set[tuple[str, str]] = set()
    for a in soup.select("a[data-probabilidad]"):
        wrap = a.parent
        if not isinstance(wrap, Tag):
            continue
        classes: list[str] = list(wrap.get("class") or [])
        pid = next((m.group(1) for c in classes if (m := _PLAYER_CLASS.match(c))), None)
        side_el = next(
            (
                p
                for p in a.parents
                if isinstance(p, Tag) and ({"local", "visitante"} & set(p.get("class") or []))
            ),
            None,
        )
        if pid is None or side_el is None:
            continue
        side = "local" if "local" in (side_el.get("class") or []) else "visitante"
        if (pid, side) in seen:
            continue
        seen.add((pid, side))
        slug_m = _PLAYER_SLUG.search(str(a.get("href", "")))
        role = str(wrap.get("data-onceff") or "").strip().lower()
        players.append(
            FFPlayer(
                player_id=pid,
                name=_player_name(wrap),
                slug=slug_m.group(1) if slug_m else None,
                side=side,
                role="titular" if role == "titular" else "suplente",
                probability=_pct(str(a.get("data-probabilidad", ""))),
                injury_code=_opt_int(str(a.get("data-lesion", ""))),
                goalkeeper="portero" in classes,
            )
        )
    if not players:
        raise FutbolFantasyParseError("partido: sin jugadores con probabilidad")
    if any(not p.name for p in players):
        raise FutbolFantasyParseError("partido: jugador sin nombre")
    return players


def _player_name(wrap: Tag) -> str:
    """El nombre va en el `alt` de la foto (ni banderas ni el icono «Más info»)."""
    for img in wrap.select("img[alt]"):
        alt = str(img.get("alt", "")).strip()
        if alt and alt != "Más info" and "flag" not in (img.get("class") or []):
            return alt
    name_el = wrap.select_one(".name")
    return name_el.get_text(" ", strip=True) if name_el else ""


def raw_players(html: str) -> list[dict[str, object]]:
    """Lo crudo que se guarda: todos los atributos de cada jugador de la ficha.

    La ficha completa pesa ~2 MB (casi todo maquetación); estos atributos son
    toda su información sobre los jugadores (~60 KB por partido).
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, object]] = []
    for a in soup.select("a[data-probabilidad]"):
        wrap = a.parent
        if not isinstance(wrap, Tag):
            continue
        attrs: dict[str, object] = {"wrapper_class": list(wrap.get("class") or [])}
        attrs.update({k: v for k, v in wrap.attrs.items() if k.startswith("data-")})
        attrs.update({k: v for k, v in a.attrs.items() if k.startswith("data-") or k == "href"})
        attrs["name"] = _player_name(wrap)
        out.append(attrs)
    return out


def _pct(text: str) -> float | None:
    m = re.fullmatch(r"\s*(\d{1,3})\s*%\s*", text)
    return int(m.group(1)) / 100 if m else None


def _opt_int(text: str) -> int | None:
    text = text.strip()
    return int(text) if re.fullmatch(r"-?\d+", text) else None
