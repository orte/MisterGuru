from __future__ import annotations

import logging

from mister_assistant.log import SecretRedactingFilter


def test_redacts_known_secrets_and_jwts() -> None:
    f = SecretRedactingFilter(["supersecreto"])
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "a=%s b=%s", ("supersecreto", "eyJa.eyJb.c1"), None
    )
    f.filter(record)
    assert record.getMessage() == "a=<redacted> b=<redacted>"
