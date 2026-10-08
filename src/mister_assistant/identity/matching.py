"""Emparejado difuso de equipos y jugadores (rapidfuzz).

Reglas:
- Equipos: alias explícitos primero; si no, el mejor por nombre, siempre que
  destaque sobre el segundo. Un empate no se resuelve solo.
- Jugadores: solo entre jugadores de Mister del mismo equipo. Se acepta solo si
  la puntuación es alta y destaca sobre el segundo candidato; lo dudoso va a la
  cola de revisión con sus candidatos.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from mister_assistant.identity.names import canonical_tokens, normalize, slug_to_name, team_key

# Nombres externos que el difuso confunde → nombre de equipo en Mister.
TEAM_ALIASES: dict[str, str] = {
    "atletico madrid": "Atlético",
    "atletico de madrid": "Atlético",
    "athletic bilbao": "Athletic Club",
    "athletic": "Athletic Club",
    "real betis": "Betis",
    "celta vigo": "Celta",
    "deportivo la coruna": "Deportivo da Coruña",
    "deportivo": "Deportivo da Coruña",
    "racing santander": "Racing de Santander",
    "racing": "Racing de Santander",
    "real madrid": "Real Madrid",
    "real sociedad": "Real Sociedad",
    "rayo": "Rayo Vallecano",
}

AUTO_THRESHOLD = 90.0
# Puntuación de «un nombre contiene al otro» o «mismos tokens con diminutivos».
SUBSET_SCORE = 95.0
REVIEW_THRESHOLD = 70.0
MIN_MARGIN = 6.0


class AmbiguousTeamError(ValueError):
    pass


def match_team(name: str, teams: dict[int, str]) -> tuple[int, float, str]:
    """Devuelve (team_id, puntuación, método). Lanza si es ambiguo o no hay candidato."""
    by_name = {normalize(v): k for k, v in teams.items()}
    alias = TEAM_ALIASES.get(normalize(name))
    if alias is not None and normalize(alias) in by_name:
        return by_name[normalize(alias)], 100.0, "alias"
    if normalize(name) in by_name:
        return by_name[normalize(name)], 100.0, "exact"
    key = team_key(name)
    scored = sorted(
        ((fuzz.token_set_ratio(key, team_key(v)), k) for k, v in teams.items()), reverse=True
    )
    if not scored:
        raise AmbiguousTeamError(f"sin equipos para emparejar {name!r}")
    best_score, best_id = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < AUTO_THRESHOLD or best_score - second < MIN_MARGIN:
        raise AmbiguousTeamError(
            f"equipo {name!r} ambiguo: " + ", ".join(f"{teams[k]} {s:.0f}" for s, k in scored[:3])
        )
    return best_id, float(best_score), "fuzzy"


@dataclass(frozen=True)
class Candidate:
    player_id: int
    name: str
    short_name: str | None
    team_id: int | None
    position: int | None


@dataclass(frozen=True)
class ExternalPlayer:
    external_id: str
    name: str
    slug: str | None
    team_id: int | None
    goalkeeper: bool | None = None


@dataclass(frozen=True)
class MatchResult:
    player_id: int | None
    score: float
    method: str  # exact | fuzzy | review | none
    candidates: list[tuple[int, str, float]] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.player_id is not None


def score_player(ext: ExternalPlayer, cand: Candidate) -> float:
    ext_names = {normalize(ext.name)}
    if ext.slug:
        ext_names.add(slug_to_name(ext.slug))
    cand_names = {normalize(cand.name)}
    if cand.short_name:
        cand_names.add(normalize(cand.short_name))
    best = 0.0
    for a in ext_names:
        for b in cand_names:
            if not a or not b:
                continue
            # token_set_ratio: «Abde» ⊂ «Abde Ezzalzouli» = 100; ratio frena los
            # subconjuntos demasiado cortos (un nombre de pila suelto).
            s = 0.7 * fuzz.token_set_ratio(a, b) + 0.3 * fuzz.ratio(a, b)
            best = max(best, s)
            # «Natan» ⊂ «Natan Souza», «Fede Valverde» = «Federico Valverde»: se acepta
            # si el token común es significativo (no un nombre de pila de 2 letras).
            ta, tb = canonical_tokens(a), canonical_tokens(b)
            small = ta if len(ta) <= len(tb) else tb
            if small and (ta <= tb or tb <= ta) and max(len(t) for t in small) >= 4:
                best = max(best, SUBSET_SCORE if ta != tb else 100.0)
    if (
        ext.goalkeeper is not None
        and cand.position is not None
        and ext.goalkeeper != (cand.position == 1)
    ):
        best -= 25.0
    return best


def match_player(ext: ExternalPlayer, candidates: list[Candidate]) -> MatchResult:
    pool = [c for c in candidates if ext.team_id is None or c.team_id == ext.team_id]
    if not pool:
        return MatchResult(None, 0.0, "none")
    exact = [c for c in pool if normalize(c.name) == normalize(ext.name)]
    if len(exact) == 1:
        return MatchResult(exact[0].player_id, 100.0, "exact")
    scored = sorted(((score_player(ext, c), c) for c in pool), key=lambda t: -t[0])
    top = [(c.player_id, c.name, round(s, 1)) for s, c in scored[:3]]
    best_score, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= AUTO_THRESHOLD and best_score - second >= MIN_MARGIN:
        return MatchResult(best.player_id, round(best_score, 1), "fuzzy", top)
    if best_score >= REVIEW_THRESHOLD:
        return MatchResult(None, round(best_score, 1), "review", top)
    return MatchResult(None, round(best_score, 1), "none", top)
