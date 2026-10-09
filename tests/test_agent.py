"""Fase 6: agente (con un cliente de Claude simulado), trazabilidad y bot."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
from test_market_report import seed

from mister_assistant.agent.runner import BETAS, MODEL, Agent, Conversation
from mister_assistant.agent.tools import TOOLS, Toolbox, ToolError
from mister_assistant.agent.traceability import answer_numbers, untraceable
from mister_assistant.config import Settings
from mister_assistant.delivery.telegram_bot import TelegramBot, handle_message, run_bot

Conn = psycopg.Connection[tuple[Any, ...]]


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool(name: str, args: dict[str, Any], tid: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=args, id=tid)


def message(content: list[Any], stop: str) -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80)
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage)


class FakeClaude:
    """Devuelve respuestas guionizadas y guarda cada petición."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs: Any) -> Any:
        # Copia superficial: el historial que se envía no debe cambiar después.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        step = self.script.pop(0)
        return step(kwargs) if callable(step) else step


# -- trazabilidad -----------------------------------------------------------------


def test_numbers_with_units_are_parsed() -> None:
    nums = dict(answer_numbers("Puja 22,27 M€; gana 2,84 pts; baja un 8,9 % en J8 (4-3-3)."))
    assert 22_270_000 in nums["22,27 M€"]
    assert 2.84 in nums["2,84"]
    assert 0.089 in [round(x, 3) for x in nums["8,9 %"]]
    assert all("J8" not in k and "4-3-3" not in k for k in nums)


def test_untraceable_flags_invented_figures() -> None:
    results = [json.dumps({"puntos": 5.37, "valor": 14384000, "variacion_7d": -0.0894})]
    ok = "Antony da 5,4 puntos, vale 14,38 M€ y se espera que baje un 8,9 %."
    assert untraceable(ok, results) == []
    bad = "Antony da 7,9 puntos."
    assert untraceable(bad, results) == ["7,9"]
    # Diferencias entre cifras de herramienta también valen.
    assert untraceable("Gana 2,1 puntos.", [json.dumps({"a": 5.4, "b": 3.3})]) == []


# -- herramientas -----------------------------------------------------------------


def test_tool_schemas_are_strict() -> None:
    for t in TOOLS:
        assert t["strict"] is True
        schema = t["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_tools_on_seeded_league(db: Conn) -> None:
    seed(db)
    tb = Toolbox(db)
    once = json.loads(tb.run("once_recomendado", {}))
    assert once["run_id"] and len(once["once"]) == 11 and once["huecos"] == 0
    hits = json.loads(tb.run("buscar_jugador", {"nombre": "J600", "equipo": None}))
    assert hits["resultados"][0]["id"] == 600
    fichaje = json.loads(tb.run("valorar_fichaje", {"player_id": 600, "precio": None}))
    assert fichaje["precio_segun"] == "en el mercado"
    assert fichaje["pujas"]["ajustada"] <= fichaje["pujas"]["segura"]
    venta = json.loads(tb.run("analizar_venta", {"player_id": 400}))
    assert "puntos_que_pierde_el_once_por_jornada" in venta
    with pytest.raises(ToolError):
        tb.run("analizar_venta", {"player_id": 600})  # no es mío
    rivales = json.loads(tb.run("estado_rivales", {}))
    assert rivales["rivales"][0]["nombre"] == "Rival"
    # La ejecución de predicciones del agente queda guardada (trazabilidad).
    run = db.execute("select trigger from prediction_runs where id = %s", (once["run_id"],))
    assert run.fetchone() == ("agente",)


def test_news_override_changes_minutes(db: Conn) -> None:
    seed(db)
    tb = Toolbox(db)
    before = tb.snap.predictions[400].p_play
    out = json.loads(
        tb.run(
            "registrar_ajuste_disponibilidad",
            {
                "player_id": 400,
                "estado": "baja",
                "p_jugar": None,
                "hasta": "2099-01-01",
                "fuente": "Rueda de prensa del entrenador",
                "fecha_fuente": "2026-10-08",
                "cita": "No estará el fin de semana",
            },
        )
    )
    assert out["registrado"]
    with pytest.raises(ToolError):
        tb.run(
            "registrar_ajuste_disponibilidad",
            {"player_id": 400, "estado": "duda", "p_jugar": 2.0, "hasta": "2099-01-01",
             "fuente": "x", "fecha_fuente": "2026-10-08", "cita": "y"},
        )  # fmt: skip
    # Una ejecución nueva lo recoge: baja = no juega.
    db.execute("update prediction_runs set created_at = created_at - interval '1 day'")
    fresh = Toolbox(db)
    assert before > 0 and fresh.snap.predictions[400].p_play == 0
    assert fresh.snap.predictions[400].components["fuente_minutos"] == "noticia: baja"


# -- bucle del agente ---------------------------------------------------------------


def test_agent_loop_runs_tools_and_answers(db: Conn) -> None:
    seed(db)

    def answer_from_tool(req: dict[str, Any]) -> Any:
        result = json.loads(req["messages"][-1]["content"][0]["content"])
        pts = str(result["puntos_esperados_once"]).replace(".", ",")
        return message([text(f"Tu once suma {pts} puntos (run {result['run_id']}).")], "end_turn")

    fake = FakeClaude([message([tool("once_recomendado", {})], "tool_use"), answer_from_tool])
    agent = Agent(db, client=fake)
    conv = Conversation()
    ans = agent.ask("¿A quién siento esta jornada?", conv)
    assert "puntos" in ans.text and ans.untraceable == []
    assert [c["tool"] for c in ans.tool_calls] == ["once_recomendado"]
    assert ans.run_id is not None and ans.usage["cache_read"] == 160
    first = fake.requests[0]
    assert first["model"] == MODEL and first["betas"] == BETAS and first["fallbacks"] == "default"
    assert first["output_config"] == {"effort": "medium"}
    # Historial solo-añadir: la segunda petición empieza exactamente como la primera.
    assert fake.requests[1]["messages"][: len(first["messages"])] == first["messages"]
    agent.log("¿A quién siento?", ans, "123")
    assert db.execute("select count(*) from agent_log").fetchone() == (1,)


def test_agent_reports_invented_numbers(db: Conn) -> None:
    seed(db)
    fake = FakeClaude([message([text("Pedri te dará 99,5 puntos.")], "end_turn")])
    ans = Agent(db, client=fake).ask("¿Pedri?")
    assert ans.untraceable == ["99,5"]


def test_tool_errors_go_back_to_the_model(db: Conn) -> None:
    seed(db)

    def check_error(req: dict[str, Any]) -> Any:
        result = req["messages"][-1]["content"][0]
        assert result["is_error"] is True and "plantilla" in result["content"]
        return message([text("Ese jugador no es tuyo.")], "end_turn")

    fake = FakeClaude(
        [message([tool("analizar_venta", {"player_id": 600})], "tool_use"), check_error]
    )
    ans = Agent(db, client=fake).ask("¿Vendo al 600?")
    assert ans.text == "Ese jugador no es tuyo." and ans.tool_calls[0]["error"]


def test_refusal_is_handled(db: Conn) -> None:
    fake = FakeClaude([message([], "refusal")])
    ans = Agent(db, client=fake).ask("algo")
    assert ans.stop_reason == "refusal" and ans.text


# -- bot ----------------------------------------------------------------------------


def tg_settings() -> Settings:
    return Settings(_env_file=None, telegram_bot_token="1:abc", telegram_chat_id="42")


def test_bot_only_answers_the_allowed_chat(db: Conn) -> None:
    sent: list[dict[str, Any]] = []
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 666}, "text": "¿quién eres?"}},
        {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/ayuda"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/getUpdates"):
            out, updates[:] = list(updates), []
            return httpx.Response(200, json={"ok": True, "result": out})
        if request.url.path.endswith("/sendMessage"):
            sent.append(body)
        return httpx.Response(200, json={"ok": True, "result": True})

    bot = TelegramBot(tg_settings(), transport=httpx.MockTransport(handler))

    def no_agent() -> Agent:
        raise AssertionError("/ayuda no llama al agente")

    run_bot(tg_settings(), no_agent, bot=bot, max_polls=2)
    assert [m["chat_id"] for m in sent] == ["42"]
    assert "Pregúntame" in sent[0]["text"]


def test_handle_message_new_conversation_and_warning(db: Conn) -> None:
    seed(db)
    conv = Conversation()
    reply, conv2 = handle_message("/nuevo", conv, lambda: Agent(db, client=FakeClaude([])), "42")
    assert reply == "Conversación nueva." and conv2 is not conv
    fake = FakeClaude([message([text("Vale 123,4 M€.")], "end_turn")])
    reply, _ = handle_message("¿cuánto vale?", conv2, lambda: Agent(db, client=fake), "42")
    assert "cifras sin comprobar" in reply


def test_agent_eval_grading() -> None:
    from mister_assistant.agent.evals import Case, grade
    from mister_assistant.agent.runner import Answer

    case = Case("¿Vendo a X?", ("buscar_jugador", "analizar_venta"), (r"run",))
    good = Answer("Según el run 5, no lo vendas.", [{"tool": "buscar_jugador"},
                                                   {"tool": "analizar_venta"}])  # fmt: skip
    assert grade(case, good) == []
    bad = Answer("No lo vendas, da 7,7 puntos.", [{"tool": "buscar_jugador"}], ["7,7"])
    problems = grade(case, bad)
    assert any("analizar_venta" in p for p in problems)
    assert any("sin respaldo" in p for p in problems) and any("run" in p for p in problems)
