"""Bucle del agente: Claude con las herramientas de solo lectura (Fase 6).

Bucle propio sobre la API de mensajes (no el tool runner, que es beta) para
controlarlo y probarlo con un cliente simulado. Historial solo-añadir: cada
respuesta se guarda tal cual (incluidos los bloques de razonamiento), nunca se
edita; cuando la conversación crece, se empieza otra.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import anthropic
import psycopg
from psycopg.types.json import Jsonb

from mister_assistant.agent.tools import TOOLS, Toolbox, ToolError
from mister_assistant.agent.traceability import untraceable

log = logging.getLogger(__name__)

Conn = psycopg.Connection[tuple[Any, ...]]
MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TOKENS = 16000
MAX_STEPS = 12
# Reintento en el servidor si el modelo declina por seguridad (lo recomienda la
# guía de la API para claude-opus-5-5).
BETAS = ["server-side-fallback-2026-07-01"]
FALLBACKS = "default"

LEAGUE_CONSTANTS = json.dumps(
    {"eur_por_punto": 100_000, "eur_por_puesto": 200_000, "hueco": -4, "clausula_extra": 0.5,
     "deuda": 0.25, "horizonte_jornadas": 5, "venta_horquilla": 0.05}
)  # fmt: skip

SYSTEM = """Eres el asistente de Jon para su equipo de Mister Fantasy (LaLiga, puntuación \
Mixta: media de AS, Marca, Mundo Deportivo y SofaScore). Respondes en español, por Telegram: \
breve, directo, sin tablas Markdown.

Reglas:
- Toda cifra que des (puntos, probabilidades, valores, pujas, saldos) sale de una \
herramienta de esta conversación. Nunca la estimes ni la inventes; si no tienes el dato, \
dilo. No hagas cuentas propias (multiplicar, convertir puntos a euros, restar): usa las \
cifras que ya dan las herramientas, que traen esas cuentas hechas.
- Las horas, en hora de Madrid, tal como las dan las herramientas.
- Antes de recomendar, consulta: once_recomendado para alineaciones, valorar_fichaje para \
fichajes y pujas, analizar_venta para ventas, estado_rivales para cláusulas y rivales.
- Si te dan un nombre, usa buscar_jugador primero. Si hay varios candidatos, pregunta cuál.
- Di de qué run de predicciones salen los puntos (run_id) y la incertidumbre que importe \
(suelo y techo, P(jugar), que los saldos rivales son estimados).
- Recomiendas; no ejecutas nada en Mister. Jon decide y lo hace él en la app.
- Si Jon te pega una noticia (rueda de prensa, parte médico), extrae para cada jugador \
afectado el estado (baja, duda, disponible), hasta cuándo, la fuente, la fecha y la frase \
literal, y regístralo con registrar_ajuste_disponibilidad. Si la noticia no es clara o no \
dice la fuente o la fecha, pregunta antes de registrar.

Reglas de la liga que importan: saldo negativo al empezar la jornada = 0 puntos; −4 por \
hueco en el once; solo las formaciones gratuitas (4-4-2, 4-5-1, 4-3-3, 3-4-3, 3-5-2, 5-4-1, \
5-3-2); 100.000 € por punto y premio por puesto en la jornada (el último cobra más); la \
cláusula por defecto es el valor de compra o de mercado + 50 %."""


@dataclass
class Answer:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    untraceable: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    run_id: int | None = None
    stop_reason: str | None = None


@dataclass
class Conversation:
    """Historial de una conversación (solo se añade, nunca se edita)."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    MAX_MESSAGES = 40

    @property
    def full(self) -> bool:
        return len(self.messages) >= self.MAX_MESSAGES


class Agent:
    def __init__(self, conn: Conn, client: Any | None = None, *, owns_conn: bool = False) -> None:
        self.conn = conn
        self._owns_conn = owns_conn
        # Inyectable: el cliente real o uno simulado en los tests.
        self.client: Any = client if client is not None else anthropic.Anthropic()
        self.tools = Toolbox(conn)

    def _create(self, messages: list[dict[str, Any]]) -> Any:
        return self.client.beta.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            betas=BETAS,
            fallbacks=FALLBACKS,
            output_config={"effort": EFFORT},
            # Sistema y herramientas son estables: se cachean entre preguntas.
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
        )

    def ask(self, question: str, conversation: Conversation | None = None) -> Answer:
        conv = conversation if conversation is not None else Conversation()
        today = date.today().isoformat()
        conv.messages.append({"role": "user", "content": f"[{today}] {question}"})
        calls: list[dict[str, Any]] = []
        results: list[str] = []
        usage = {"input": 0, "output": 0, "cache_read": 0}
        answer_text = ""
        stop: str | None = None
        for _ in range(MAX_STEPS):
            resp = self._create(conv.messages)
            u = getattr(resp, "usage", None)
            if u is not None:
                usage["input"] += int(getattr(u, "input_tokens", 0) or 0)
                usage["output"] += int(getattr(u, "output_tokens", 0) or 0)
                usage["cache_read"] += int(getattr(u, "cache_read_input_tokens", 0) or 0)
            conv.messages.append({"role": "assistant", "content": resp.content})
            stop = resp.stop_reason
            if stop == "refusal":
                answer_text = "No puedo responder a eso."
                break
            if stop == "pause_turn":
                continue
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if stop != "tool_use" or not tool_uses:
                answer_text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                if stop == "max_tokens":
                    answer_text += "\n(respuesta cortada)"
                break
            tool_results = []
            for tu in tool_uses:
                args = tu.input if isinstance(tu.input, dict) else json.loads(tu.input)
                try:
                    out = self.tools.run(tu.name, args)
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": tu.id, "content": out}
                    )
                    results.append(out)
                    calls.append({"tool": tu.name, "input": args})
                except (ToolError, ValueError, TypeError) as exc:
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": tu.id, "content": str(exc),
                         "is_error": True}
                    )  # fmt: skip
                    calls.append({"tool": tu.name, "input": args, "error": str(exc)})
            # Todos los resultados en un solo mensaje (llamadas en paralelo).
            conv.messages.append({"role": "user", "content": tool_results})
        else:
            answer_text = "He necesitado demasiados pasos; pregúntamelo de otra forma."
        run_id = self.tools._snap.run_id if self.tools._snap else None
        # Las constantes de la liga del prompt de sistema también son cifras con respaldo.
        sources = [*results, LEAGUE_CONSTANTS]
        return Answer(answer_text, calls, untraceable(answer_text, sources), usage, run_id, stop)

    def close(self) -> None:
        if self._owns_conn:
            self.conn.close()

    def log(self, question: str, answer: Answer, chat_id: str | None = None) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "insert into agent_log (chat_id, question, answer, tool_calls, untraceable,"
                " run_id, usage) values (%s, %s, %s, %s, %s, %s, %s)",
                (chat_id, question, answer.text, Jsonb(answer.tool_calls),
                 Jsonb(answer.untraceable), answer.run_id, Jsonb(answer.usage)),
            )  # fmt: skip
