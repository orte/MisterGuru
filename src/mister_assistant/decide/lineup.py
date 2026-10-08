"""Once óptimo.

El objetivo (suma de puntos esperados) es separable por jugador, así que para
cada formación permitida basta con elegir el mejor portero y los mejores de
cada línea: es el óptimo exacto, sin necesidad de programación entera. Se
prueban todas las formaciones y gana la de mayor suma.

Reglas de la liga: siempre 11 (-4 por hueco), 1 portero y solo las formaciones
gratuitas. Los cambios durante la jornada no existen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Formaciones permitidas: solo las gratuitas. Mister lista 12 en
# `sport.parameters.formations`, pero 4-2-4, 4-6-0, 3-3-4, 3-6-1 y 5-5-0 son de
# pago (`_FG_data.formations` de /team: free=false, 50 créditos) y la liga no
# permite compras (`purchase_formations = 0`). Ver docs/league-rules.md.
FORMATIONS: tuple[str, ...] = ("4-4-2", "4-5-1", "4-3-3", "3-4-3", "3-5-2", "5-4-1", "5-3-2")
PAID_FORMATIONS: tuple[str, ...] = ("4-2-4", "4-6-0", "3-3-4", "3-6-1", "5-5-0")
EMPTY_SLOT_PENALTY = -4.0
FRAGILE_P_PLAY = 0.70


@dataclass(frozen=True)
class Option:
    player_id: int
    name: str
    position: int  # 1 POR, 2 DEF, 3 CEN, 4 DEL
    exp_points: float
    p_play: float
    p20: float = 0.0
    p80: float = 0.0


@dataclass(frozen=True)
class FragileSlot:
    player_id: int
    p_play: float
    replacement_id: int | None  # quién entraría si se le quita
    cost: float  # puntos esperados que se pierden con el cambio (negativo = se gana)


@dataclass
class Lineup:
    formation: str
    players: list[Option]
    empty_slots: int = 0
    fragile: list[FragileSlot] = field(default_factory=list)

    @property
    def expected_points(self) -> float:
        return sum(p.exp_points for p in self.players) + EMPTY_SLOT_PENALTY * self.empty_slots

    @property
    def player_ids(self) -> list[int]:
        return [p.player_id for p in self.players]


def _counts(formation: str) -> dict[int, int]:
    d, m, f = (int(x) for x in formation.split("-"))
    return {1: 1, 2: d, 3: m, 4: f}


def best_lineup(
    squad: list[Option],
    formations: tuple[str, ...] = FORMATIONS,
    exclude: frozenset[int] = frozenset(),
) -> Lineup:
    """Mejor once posible; si ninguna formación se puede completar, el menos malo."""
    by_pos: dict[int, list[Option]] = {1: [], 2: [], 3: [], 4: []}
    for o in squad:
        if o.player_id not in exclude:
            by_pos[o.position].append(o)
    for pos in by_pos:
        # Desempate determinista: más puntos, más P(jugar), menor id.
        by_pos[pos].sort(key=lambda o: (-o.exp_points, -o.p_play, o.player_id))

    best: Lineup | None = None
    for formation in formations:
        need = _counts(formation)
        chosen: list[Option] = []
        empty = 0
        for pos, n in need.items():
            picks = by_pos[pos][:n]
            chosen.extend(picks)
            empty += n - len(picks)
        cand = Lineup(formation, chosen, empty)
        if best is None or (cand.empty_slots, -cand.expected_points) < (
            best.empty_slots, -best.expected_points,
        ):  # fmt: skip
            best = cand
    assert best is not None
    return best


def recommend(
    squad: list[Option],
    formations: tuple[str, ...] = FORMATIONS,
    fragile_below: float = FRAGILE_P_PLAY,
) -> Lineup:
    """Once óptimo y, para cada titular dudoso, quién entraría y cuánto cuesta."""
    lineup = best_lineup(squad, formations)
    for p in lineup.players:
        if p.p_play >= fragile_below:
            continue
        alt = best_lineup(squad, formations, exclude=frozenset({p.player_id}))
        incoming = [o for o in alt.players if o.player_id not in lineup.player_ids]
        lineup.fragile.append(
            FragileSlot(
                player_id=p.player_id,
                p_play=p.p_play,
                replacement_id=incoming[0].player_id if incoming else None,
                cost=round(lineup.expected_points - alt.expected_points, 2),
            )
        )
    return lineup


def evaluate(squad: list[Option], player_ids: list[int]) -> float:
    """Puntos esperados de un once dado (p. ej. el que está puesto en la app)."""
    by_id = {o.player_id: o for o in squad}
    picked = [by_id[i] for i in player_ids if i in by_id]
    return sum(o.exp_points for o in picked) + EMPTY_SLOT_PENALTY * max(0, 11 - len(picked))
