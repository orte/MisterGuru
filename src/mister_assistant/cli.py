"""Punto de entrada `mister-assistant`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from mister_assistant.config import load_settings
from mister_assistant.jobs.doctor import format_report, run_doctor
from mister_assistant.log import setup_logging


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mister-assistant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Valida credenciales de Mister y lista las ligas")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.log_level, settings.secret_values())

    if args.command == "doctor":
        report = run_doctor(settings)
        print(format_report(report))
        return report.exit_code
    return 1


if __name__ == "__main__":
    sys.exit(main())
