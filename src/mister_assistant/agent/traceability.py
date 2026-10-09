"""¿Salen de las herramientas las cifras de una respuesta? (PLAN Fase 6)

Se extraen los números de la respuesta (con sus unidades: «4,3 M€», «900 k€»,
«66 %», «+1,5 pts») y se buscan, con la tolerancia del redondeo, entre todos los
números de los resultados de las herramientas. Los enteros pequeños (puestos,
jornadas, formaciones, conteos) no se comprueban: son demasiado comunes.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable
from typing import Any

_NUM = re.compile(
    r"(?<![\w.,])([+\-−]?\d{1,3}(?:\.\d{3})+(?:,\d+)?|[+\-−]?\d+(?:[.,]\d+)?)\s*(M€|M|k€|k|%|€)?",
    re.IGNORECASE,
)
SMALL_INT = 15


def _to_float(text: str) -> float:
    t = text.replace("−", "-")
    if re.fullmatch(r"[+\-]?\d{1,3}(?:\.\d{3})+(?:,\d+)?", t):
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    return float(t)


def answer_numbers(text: str) -> list[tuple[str, list[float]]]:
    """(texto, valores candidatos) de cada cifra de la respuesta."""
    # Fuera formaciones (4-3-3), jornadas (J8) y fechas, que no son cifras de herramienta.
    clean = re.sub(r"\b\d-\d-\d\b|\bJ\d+\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", text)
    out = []
    for m in _NUM.finditer(clean):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        try:
            x = _to_float(raw)
        except ValueError:
            continue
        if unit == "" and float(x).is_integer() and abs(x) <= SMALL_INT:
            continue
        cands = [x, abs(x)]
        if unit in ("m€", "m"):
            cands += [x * 1_000_000, abs(x) * 1_000_000]
        elif unit in ("k€", "k"):
            cands += [x * 1_000, abs(x) * 1_000]
        elif unit == "%":
            cands += [x / 100, abs(x) / 100]
        out.append((m.group(0).strip(), cands))
    return out


def tool_numbers(results: Iterable[str]) -> list[float]:
    nums: list[float] = []

    def walk(o: Any) -> None:
        if isinstance(o, bool):
            return
        if isinstance(o, int | float):
            nums.append(float(o))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            for m in _NUM.finditer(o):
                with contextlib.suppress(ValueError):
                    nums.append(_to_float(m.group(1)))

    for r in results:
        try:
            walk(json.loads(r))
        except ValueError:
            walk(r)
    return nums


def _close(a: float, b: float) -> bool:
    # Tolerancia del redondeo al presentar: 2 decimales o un 1,5 % relativo.
    return abs(a - b) <= max(0.051, 0.015 * abs(b))


def untraceable(answer: str, results: Iterable[str]) -> list[str]:
    """Cifras de la respuesta que no aparecen en ninguna herramienta."""
    # En valor absoluto: «baja un 8,9 %» corresponde a −0,089.
    pool = [abs(x) for x in tool_numbers(list(results))]
    # Diferencias y sumas sencillas entre dos cifras de herramienta también valen
    # (p. ej. «gana 2,1 puntos» = 5,4 − 3,3).
    derived = {round(abs(a - b), 2) for a in pool[:400] for b in pool[:400]}
    derived |= {round(a + b, 2) for a in pool[:200] for b in pool[:200]}
    missing = []
    for text, cands in answer_numbers(answer):
        if any(_close(abs(c), t) for c in cands for t in pool):
            continue
        if any(_close(abs(c), d) for c in cands for d in derived):
            continue
        missing.append(text)
    return missing
