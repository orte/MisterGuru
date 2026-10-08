"""Settings de la aplicación, cargados de variables de entorno y `.env`."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REQUIRED_SECRETS: tuple[str, ...] = ("mister_token", "mister_x_auth", "mister_phpsessid")


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

    log_level: str = "INFO"

    def missing_secrets(self) -> list[str]:
        """Nombres de variables obligatorias sin valor (nunca sus valores)."""
        return [name.upper() for name in REQUIRED_SECRETS if not _has_value(getattr(self, name))]

    def secret_values(self) -> list[str]:
        """Valores secretos presentes, para redactarlos de los logs."""
        values: list[str] = []
        for name in (*REQUIRED_SECRETS, "mister_refresh_token"):
            secret = getattr(self, name)
            if _has_value(secret):
                values.append(secret.get_secret_value())
        return values


def _has_value(secret: SecretStr | None) -> bool:
    return secret is not None and secret.get_secret_value().strip() != ""


def load_settings() -> Settings:
    return Settings()
