from __future__ import annotations

import pytest

from mister_assistant.config import Settings


def test_missing_secrets_lists_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MISTER_TOKEN", "MISTER_X_AUTH", "MISTER_PHPSESSID", "MISTER_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None, mister_x_auth="  ")
    assert s.missing_secrets() == ["MISTER_TOKEN", "MISTER_X_AUTH", "MISTER_PHPSESSID"]


def test_reads_env_and_hides_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MISTER_TOKEN", "tok-123456")
    monkeypatch.setenv("MISTER_X_AUTH", "xa-123456")
    monkeypatch.setenv("MISTER_PHPSESSID", "sess-123456")
    s = Settings(_env_file=None)
    assert s.missing_secrets() == []
    assert "tok-123456" not in repr(s)
    assert set(s.secret_values()) >= {"tok-123456", "xa-123456", "sess-123456"}


def test_secrets_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Así llegan a veces desde GitHub Secrets: con salto de línea final.
    monkeypatch.setenv("MISTER_X_AUTH", "abc123\n")
    monkeypatch.setenv("MISTER_TOKEN", "  tok-123456 ")
    monkeypatch.setenv("MISTER_PHPSESSID", "sess\r\n")
    s = Settings(_env_file=None)
    assert s.mister_x_auth is not None and s.mister_x_auth.get_secret_value() == "abc123"
    assert s.mister_token is not None and s.mister_token.get_secret_value() == "tok-123456"
    assert s.invalid_header_secrets() == []


def test_invalid_header_characters_are_reported_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MISTER_X_AUTH", "abc\ndef")
    monkeypatch.setenv("MISTER_TOKEN", "tok")
    monkeypatch.setenv("MISTER_PHPSESSID", 'ses"s ion')
    s = Settings(_env_file=None)
    assert s.invalid_header_secrets() == ["MISTER_X_AUTH", "MISTER_PHPSESSID"]
