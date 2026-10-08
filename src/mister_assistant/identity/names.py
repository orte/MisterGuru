"""Normalización de nombres para el emparejado."""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Palabras que no distinguen equipos («Real», «CF», «Club»…).
_TEAM_NOISE = frozenset(
    {"real", "club", "cf", "fc", "cd", "ud", "sd", "rcd", "rc", "de", "del", "la", "el", "sad"}
)


def normalize(name: str) -> str:
    """«Álvaro Vallés-Pérez» → «alvaro valles perez»."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", ascii_only.lower()).strip()


# Diminutivos habituales → forma canónica (se aplica a cada token).
NICKNAMES: dict[str, str] = {
    "alex": "alejandro", "fede": "federico", "fer": "fernando", "nando": "fernando",
    "rafa": "rafael", "rafel": "rafael", "dani": "daniel", "javi": "javier",
    "nico": "nicolas", "manu": "manuel", "toni": "antonio", "edu": "eduardo",
    "santi": "santiago", "isma": "ismael", "juanmi": "juan miguel", "pepe": "jose",
    "chema": "jose maria", "nacho": "ignacio", "paco": "francisco", "fran": "francisco",
    "kike": "enrique", "quique": "enrique", "jony": "jonathan", "jonny": "jonathan",
    "migue": "miguel", "lucho": "luis", "joselu": "jose luis", "vitolo": "victor",
    "adri": "adrian", "alvarito": "alvaro", "sergi": "sergio", "pau": "pablo",
}  # fmt: skip


def canonical_tokens(name: str) -> frozenset[str]:
    """Tokens normalizados con diminutivos expandidos: «Fede Valverde» → {federico, valverde}."""
    out: set[str] = set()
    for tok in normalize(name).split():
        out.update(NICKNAMES.get(tok, tok).split())
    return frozenset(out)


def slug_to_name(slug: str) -> str:
    """«raul-garcia-1» → «raul garcia» (quita sufijos numéricos de desempate)."""
    return re.sub(r"(\s\d+)+$", "", slug.replace("-", " ")).strip()


def team_key(name: str) -> str:
    words = [w for w in normalize(name).split() if w not in _TEAM_NOISE]
    return " ".join(words) or normalize(name)
