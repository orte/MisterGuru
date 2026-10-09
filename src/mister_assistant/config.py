"""Settings de la aplicación, cargados de variables de entorno y `.env`."""

from __future__ import annotations

import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REQUIRED_SECRETS: tuple[str, ...] = ("mister_token", "mister_x_auth", "mister_phpsessid")
# Secrets que viajan en cabeceras o cookies HTTP: solo ASCII visible, sin espacios.
HEADER_SECRETS: tuple[str, ...] = (*REQUIRED_SECRETS, "mister_refresh_token")
_HEADER_SAFE = re.compile(r"[\x21-\x7e]*")


class ConfigError(Exception):
    """Configuración ausente o inválida (mensaje sin valores secretos)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mister_base_url: str = "https://mister.mundodeportivo.com"
    mister_token: SecretStr | None = None
    mister_x_auth: SecretStr | None = None
    mister_phpsessid: SecretStr | None = None
    mister_refresh_token: SecretStr | None = None

    # Espera mínima entre peticiones a Mister, más un jitter aleatorio.
    mister_min_interval_s: float = Field(default=2.5, ge=0)
    mister_jitter_s: float = Field(default=1.0, ge=0)
    mister_timeout_s: float = Field(default=20.0, gt=0)

    # Cadena de conexión de Postgres (Supabase → Connect → Session pooler).
    database_url: SecretStr | None = None

    # API de Anthropic para el agente (Fase 6).
    anthropic_api_key: SecretStr | None = None

    # The Odds API (plan gratuito: 500 créditos/mes). Sin clave no se piden cuotas.
    odds_api_key: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    log_level: str = "INFO"

    @field_validator(
        "mister_token", "mister_x_auth", "mister_phpsessid", "mister_refresh_token",
        "database_url", "odds_api_key", "telegram_bot_token", "anthropic_api_key",
        mode="before",
    )  # fmt: skip
    @classmethod
    def _strip(cls, value: object) -> object:
        # Un secret pegado en GitHub con un salto de línea final rompe la cabecera
        # HTTP (h11 LocalProtocolError). Se recortan espacios y saltos en los extremos.
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, SecretStr):
            return SecretStr(value.get_secret_value().strip())
        return value

    def invalid_header_secrets(self) -> list[str]:
        """Nombres de secrets con caracteres que no caben en una cabecera HTTP."""
        bad = []
        for name in HEADER_SECRETS:
            secret = getattr(self, name)
            if _has_value(secret) and not _HEADER_SAFE.fullmatch(secret.get_secret_value()):
                bad.append(name.upper())
        return bad

    def database_dsn(self) -> str:
        """DSN de Postgres validado. Lanza `ConfigError` con instrucciones si no sirve."""
        if not _has_value(self.database_url):
            raise ConfigError("Falta DATABASE_URL en el entorno")
        assert self.database_url is not None
        dsn = self.database_url.get_secret_value().strip()
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ConfigError(
                "DATABASE_URL debe ser una cadena de conexión de Postgres "
                "(postgresql://usuario:contraseña@host:puerto/postgres), no la URL de la API "
                "de Supabase (https://….supabase.co). Cópiala de Supabase → Connect → "
                "Session pooler (compatible con IPv4)."
            )
        return dsn

    def telegram_enabled(self) -> bool:
        return _has_value(self.telegram_bot_token) and bool(self.telegram_chat_id)

    def missing_secrets(self) -> list[str]:
        """Nombres de variables obligatorias sin valor (nunca sus valores)."""
        return [name.upper() for name in REQUIRED_SECRETS if not _has_value(getattr(self, name))]

    def secret_values(self) -> list[str]:
        """Valores secretos presentes, para redactarlos de los logs."""
        values: list[str] = []
        for name in (
            *REQUIRED_SECRETS,
            "mister_refresh_token",
            "database_url",
            "telegram_bot_token",
            "odds_api_key",
            "anthropic_api_key",
        ):
            secret = getattr(self, name)
            if _has_value(secret):
                values.append(secret.get_secret_value())
        return values


def _has_value(secret: SecretStr | None) -> bool:
    return secret is not None and secret.get_secret_value().strip() != ""


def load_settings() -> Settings:
    return Settings()
