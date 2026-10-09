"""Bot de Telegram del agente (Fase 6): long polling, solo el chat de Jon.

`mister-assistant bot` deja un proceso escuchando (getUpdates): no hace falta
servidor web ni puertos abiertos. Cualquier mensaje de otro chat se ignora.
Comandos: /nuevo (empieza conversación), /ayuda.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

import anthropic
import httpx

from mister_assistant.agent.runner import Agent, Conversation
from mister_assistant.config import Settings
from mister_assistant.delivery.telegram import API, MAX_LEN

log = logging.getLogger(__name__)

HELP = (
    "Pregúntame por tu equipo. Por ejemplo:\n"
    "- ¿A quién siento esta jornada?\n"
    "- ¿Vendo a Antony?\n"
    "- ¿Pujo por Pedri y cuánto?\n"
    "- ¿Quién me puede clausular?\n"
    "Pégame una noticia (rueda de prensa, parte médico) con su fuente y fecha y la registro.\n"
    "/nuevo empieza una conversación nueva."
)


class TelegramBot:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.telegram_enabled():
            raise ValueError("faltan TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID")
        assert settings.telegram_bot_token is not None
        self._base = f"{API}/bot{settings.telegram_bot_token.get_secret_value()}"
        self.allowed_chat = str(settings.telegram_chat_id)
        self._http = httpx.Client(timeout=70, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _post(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            resp = self._http.post(f"{self._base}/{method}", json=payload)
        except httpx.HTTPError as exc:
            # Sin el token en el mensaje (va en la URL).
            raise ConnectionError(f"Telegram {method}: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise ConnectionError(f"Telegram {method} respondió {resp.status_code}")
        return resp.json().get("result")

    def updates(self, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return list(self._post("getUpdates", payload) or [])

    def send(self, chat_id: str, text: str) -> None:
        for i in range(0, max(len(text), 1), MAX_LEN):
            self._post(
                "sendMessage",
                {"chat_id": chat_id, "text": text[i : i + MAX_LEN] or "…",
                 "disable_web_page_preview": True},
            )  # fmt: skip

    def typing(self, chat_id: str) -> None:
        with contextlib.suppress(ConnectionError):
            self._post("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def handle_message(
    text: str,
    conversation: Conversation,
    agent_factory: Callable[[], Agent],
    chat_id: str,
) -> tuple[str, Conversation]:
    """Responde a un mensaje. Devuelve (respuesta, conversación a usar después)."""
    cmd = text.strip().lower()
    if cmd in ("/start", "/ayuda", "/help"):
        return HELP, conversation
    if cmd == "/nuevo":
        return "Conversación nueva.", Conversation()
    if conversation.full:
        conversation = Conversation()
    agent = agent_factory()
    try:
        try:
            answer = agent.ask(text, conversation)
        except anthropic.BadRequestError as exc:
            # Un historial que la API ya no acepta: se empieza otra conversación.
            log.warning("petición rechazada (%s); conversación nueva", exc.status_code)
            conversation = Conversation()
            answer = agent.ask(text, conversation)
        agent.log(text, answer, chat_id)
    finally:
        agent.close()
    reply = answer.text or "No tengo respuesta."
    if answer.untraceable:
        log.warning("cifras sin respaldo en herramientas: %s", answer.untraceable)
        reply += "\n\n(⚠️ cifras sin comprobar: " + ", ".join(answer.untraceable[:5]) + ")"
    return reply, conversation


def run_bot(
    settings: Settings,
    agent_factory: Callable[[], Agent],
    *,
    bot: TelegramBot | None = None,
    max_polls: int | None = None,
) -> None:
    bot = bot or TelegramBot(settings)
    conversation = Conversation()
    offset: int | None = None
    polls = 0
    log.info("bot escuchando (solo el chat %s)", bot.allowed_chat)
    while max_polls is None or polls < max_polls:
        polls += 1
        try:
            updates = bot.updates(offset)
        except ConnectionError as exc:
            log.warning("%s; reintento en 10 s", exc)
            time.sleep(10)
            continue
        for up in updates:
            offset = int(up["update_id"]) + 1
            msg = up.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text")
            if chat_id != bot.allowed_chat:
                log.warning("mensaje de un chat no autorizado (%s): ignorado", chat_id)
                continue
            if not text:
                continue
            bot.typing(chat_id)
            try:
                reply, conversation = handle_message(text, conversation, agent_factory, chat_id)
            except (anthropic.APIError, ValueError) as exc:
                log.error("error respondiendo: %s", type(exc).__name__)
                reply = f"No he podido responder ({type(exc).__name__}). Prueba otra vez."
            bot.send(chat_id, reply)
