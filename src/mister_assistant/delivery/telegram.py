"""Avisos por Telegram (Bot API). Solo envía al chat configurado."""

from __future__ import annotations

import logging

import httpx

from mister_assistant.config import Settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_LEN = 4000  # límite de Telegram: 4096 caracteres


class TelegramError(Exception):
    """No se pudo enviar el mensaje (sin incluir el token en el texto)."""


def send_message(
    settings: Settings, text: str, *, transport: httpx.BaseTransport | None = None
) -> bool:
    """Envía `text`. Devuelve False si Telegram no está configurado."""
    if not settings.telegram_enabled():
        log.warning("Telegram no configurado: no se envía el aviso")
        return False
    assert settings.telegram_bot_token is not None
    token = settings.telegram_bot_token.get_secret_value()
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 1] + "…"
    try:
        with httpx.Client(timeout=15, transport=transport) as client:
            resp = client.post(
                f"{API}/bot{token}/sendMessage",
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
    except httpx.HTTPError as exc:
        raise TelegramError(f"error de red con Telegram: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise TelegramError(f"Telegram respondió {resp.status_code}")
    return True
