"""Configuración de logging con redacción de secretos."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

REDACTED = "<redacted>"
# Cadenas que parecen tokens (JWT u hashes largos) se redactan aunque no se conozcan.
_TOKENISH = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+|[A-Fa-f0-9]{32,}")


class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if len(s) >= 6}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return _TOKENISH.sub(REDACTED, text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        return True


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(SecretRedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx registra URLs y cabeceras a nivel DEBUG/INFO; no lo queremos en los logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
