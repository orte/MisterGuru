"""Mercado: valoración de fichajes, pujas recomendadas, cláusulas y ventas (Fase 4).

Idea central: el dinero invertido en un jugador no se pierde, porque se
recupera vendiéndolo a su valor de mercado (el juego ofrece ±5 %). El coste
real de un fichaje es lo que se paga por encima de su valor más lo que se
espera que pierda, menos lo que gane. Lo que se compra son puntos: los que
añade al once frente a quien sustituiría.

    puntuación = puntos extra en el horizonte
                 + (variación de valor esperada − sobreprecio) / €_por_punto

`€_por_punto` = 100.000 € (la bonificación de la liga por punto): convierte el
dinero en puntos al tipo que paga la propia liga.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import quantiles

from mister_assistant.decide.lineup import FORMATIONS, Option, best_lineup

HORIZON_GAMEWEEKS = 5
EUR_PER_POINT = 100_000
SALE_SPREAD = 0.025  # se vende al valor ±5 %: de media se pierde la mitad
# Escalones de subida de cláusula (§2.4): multiplicador sobre la base.
CLAUSE_STEPS = {1: 2.0, 2: 2.5, 3: 3.0}


@dataclass(frozen=True)
class BidLevels:
    """Pujas como múltiplo del valor, según las pujas ganadas en la liga."""

    tight: float  # «ajustada»: mediana de lo pagado
    likely: float  # «probable»: percentil 75
    safe: float  # «segura»: percentil 90
    samples: int

    def amounts(self, value: int) -> tuple[int, int, int]:
        return tuple(_round_bid(value * r) for r in (self.tight, self.likely, self.safe))  # type: ignore[return-value]


DEFAULT_BIDS = BidLevels(1.02, 1.10, 1.25, 0)


def _round_bid(amount: float) -> int:
    return int(-(-amount // 1000) * 1000)  # al millar, hacia arriba


def bid_levels(ratios: list[float], min_samples: int = 15) -> BidLevels:
    """Niveles a partir de precio/valor de las compras ganadas al mercado del juego."""
    clean = sorted(r for r in ratios if 0.8 <= r <= 3.0)
    if len(clean) < min_samples:
        return DEFAULT_BIDS
    q = quantiles(clean, n=20, method="inclusive")  # q[9]=p50, q[14]=p75, q[17]=p90
    return BidLevels(max(1.0, q[9]), max(1.0, q[14]), max(1.0, q[17]), len(clean))


@dataclass(frozen=True)
class Candidate:
    player_id: int
    name: str
    position: int
    exp_points: float  # próxima jornada
    p_play: float
    value: int
    price: int  # precio de venta (mercado) o cláusula
    change_14d: float  # fracción esperada
    seller_id: int | None = None


@dataclass
class Valuation:
    candidate: Candidate
    marginal_points: float  # por jornada, frente al once actual
    horizon_points: float
    expected_value_change: int  # € en 14 días
    premium: int  # € por encima del valor
    score: float
    replaces: list[int] = field(default_factory=list)
    affordable_now: bool = True
    needs_sales: int = 0


def marginal_points(
    squad: list[Option], extra: Option, formations: tuple[str, ...] = FORMATIONS
) -> tuple[float, list[int]]:
    base = best_lineup(squad, formations)
    with_extra = best_lineup([*squad, extra], formations)
    out = [p for p in base.player_ids if p not in with_extra.player_ids]
    return with_extra.expected_points - base.expected_points, out


def value(
    squad: list[Option],
    cand: Candidate,
    *,
    balance: int,
    horizon: int = HORIZON_GAMEWEEKS,
) -> Valuation:
    opt = Option(cand.player_id, cand.name, cand.position, cand.exp_points, cand.p_play)
    delta, replaced = marginal_points(squad, opt)
    horizon_pts = delta * horizon
    dv = round(cand.value * cand.change_14d)
    premium = max(0, cand.price - cand.value) + round(cand.value * SALE_SPREAD)
    score = horizon_pts + (dv - premium) / EUR_PER_POINT
    return Valuation(
        candidate=cand,
        marginal_points=round(delta, 2),
        horizon_points=round(horizon_pts, 2),
        expected_value_change=dv,
        premium=premium,
        score=round(score, 2),
        replaces=replaced,
        affordable_now=cand.price <= balance,
        needs_sales=max(0, cand.price - balance),
    )


# -- cláusulas -------------------------------------------------------------------


@dataclass(frozen=True)
class ClauseRaise:
    step: int
    new_clause: int
    cost: int


def clause_raises(floor: int, current_clause: int) -> list[ClauseRaise]:
    """Subidas posibles (§2.4): coste = 20 % de la cláusula máxima / 3 × escalón.

    Cláusula máxima = base × 3 (+200 %), así que el coste es 20 % de la base por
    escalón. Solo se listan los escalones por encima de la cláusula actual.
    """
    max_clause = floor * CLAUSE_STEPS[3]
    return [
        ClauseRaise(step, round(floor * mult), round(0.20 * max_clause / 3 * step))
        for step, mult in CLAUSE_STEPS.items()
        if floor * mult > current_clause
    ]


@dataclass
class Exposure:
    player_id: int
    name: str
    clause: int
    marginal_loss: float  # puntos por jornada que se perderían
    threats: list[tuple[int, str, int]]  # (mánager, nombre, gasto estimado posible)
    raise_to: ClauseRaise | None


def exposure(
    player: Option,
    clause: int,
    floor: int,
    squad: list[Option],
    rivals: list[tuple[int, str, int]],
) -> Exposure | None:
    """Jugador propio que algún rival podría clausular con su gasto estimado."""
    threats = sorted((r for r in rivals if r[2] >= clause), key=lambda r: -r[2])
    if not threats:
        return None
    base = best_lineup(squad)
    without = best_lineup(squad, exclude=frozenset({player.player_id}))
    loss = base.expected_points - without.expected_points
    top = threats[0][2]
    raise_to = next((r for r in clause_raises(floor, clause) if r.new_clause > top), None)
    return Exposure(player.player_id, player.name, clause, round(loss, 2), threats, raise_to)
