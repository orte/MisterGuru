from __future__ import annotations

import json

import httpx
import pytest

from mister_assistant.config import Settings
from mister_assistant.delivery.telegram import TelegramError, send_message


def tg_settings() -> Settings:
    return Settings(_env_file=None, telegram_bot_token="123:secreto", telegram_chat_id="42")


def test_sends_to_configured_chat() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    assert send_message(tg_settings(), "hola", transport=httpx.MockTransport(handler))
    assert seen[0].url.path == "/bot123:secreto/sendMessage"
    assert json.loads(seen[0].content)["chat_id"] == "42"


def test_error_does_not_leak_token() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(401))
    with pytest.raises(TelegramError) as exc:
        send_message(tg_settings(), "hola", transport=transport)
    assert "secreto" not in str(exc.value)


def test_not_configured_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert send_message(Settings(_env_file=None), "hola") is False


def test_database_dsn_rejects_supabase_api_url() -> None:
    from mister_assistant.config import ConfigError

    s = Settings(_env_file=None, database_url="https://abc.supabase.co")
    with pytest.raises(ConfigError, match="Session pooler"):
        s.database_dsn()
    ok = Settings(_env_file=None, database_url="postgresql://u:p@h:5432/postgres")
    assert ok.database_dsn().startswith("postgresql://")
