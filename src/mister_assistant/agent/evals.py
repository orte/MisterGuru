"""Casos de prueba del agente con el modelo real (PLAN Fase 6).

Cada caso es una pregunta tipo con lo que la respuesta tiene que cumplir:
herramientas que debe usar, que todas sus cifras salgan de ellas y que cite el
run de predicciones. Cuesta dinero de la API: se ejecuta a mano con
`mister-assistant agent-eval`, no en los tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mister_assistant.agent.runner import Agent, Answer

Conn = Any
# Opus 5.5: $4 / $20 por millón de tokens; lecturas de caché $0,20.
PRICE_INPUT = 4.0 / 1e6
PRICE_CACHE_READ = 0.20 / 1e6
PRICE_OUTPUT = 20.0 / 1e6


@dataclass(frozen=True)
class Case:
    question: str
    must_use: tuple[str, ...]
    must_mention: tuple[str, ...] = ()  # expresiones regulares que debe contener


CASES: tuple[Case, ...] = (
    Case("¿A quién siento esta jornada?", ("once_recomendado",), (r"run",)),
    Case("¿Vendo a Antony?", ("buscar_jugador", "analizar_venta"), (r"run",)),
    Case("¿Pujo por Pedri y cuánto?", ("buscar_jugador", "valorar_fichaje"), (r"M€|€",)),
    Case("¿Quién me puede clausular a algún titular?", ("estado_rivales",), (r"estimad",)),
    Case(
        "¿Qué es mejor esta jornada, Antony o Pedri?",
        ("buscar_jugador", "comparar_jugadores"),
        (),
    ),
)


@dataclass
class CaseResult:
    case: Case
    answer: Answer
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems

    @property
    def cost(self) -> float:
        u = self.answer.usage
        uncached = max(0, u.get("input", 0) - u.get("cache_read", 0))
        return (
            uncached * PRICE_INPUT
            + u.get("cache_read", 0) * PRICE_CACHE_READ
            + u.get("output", 0) * PRICE_OUTPUT
        )


def grade(case: Case, answer: Answer) -> list[str]:
    used = {c["tool"] for c in answer.tool_calls}
    problems = [f"no usó {t}" for t in case.must_use if t not in used]
    if answer.untraceable:
        problems.append(f"cifras sin respaldo: {answer.untraceable}")
    for pattern in case.must_mention:
        if not re.search(pattern, answer.text, re.IGNORECASE):
            problems.append(f"no menciona /{pattern}/")
    if not answer.text.strip():
        problems.append("respuesta vacía")
    return problems


def run_cases(agent_factory: Any, cases: tuple[Case, ...] = CASES) -> list[CaseResult]:
    out = []
    for case in cases:
        agent: Agent = agent_factory()
        try:
            answer = agent.ask(case.question)
            agent.log(case.question, answer, "agent-eval")
        finally:
            agent.close()
        out.append(CaseResult(case, answer, grade(case, answer)))
    return out


def format_results(results: list[CaseResult]) -> str:
    lines = []
    for r in results:
        mark = "✓" if r.passed else "✗"
        tools = ", ".join(c["tool"] for c in r.answer.tool_calls)
        lines.append(f"{mark} {r.case.question}  [{tools}] · {r.cost:.3f} $")
        lines += [f"    - {p}" for p in r.problems]
        lines += ["    " + ln for ln in r.answer.text.splitlines()[:8]]
    passed = sum(r.passed for r in results)
    total = sum(r.cost for r in results)
    lines.append(f"\n{passed}/{len(results)} casos correctos · coste total {total:.3f} $")
    return "\n".join(lines)
