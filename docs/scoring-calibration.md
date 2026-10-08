# Calibración del motor de puntuación Mixta

Motor: `src/mister_assistant/scoring/mixed.py`. Calibración:
`src/mister_assistant/scoring/calibration.py`, que se ejecuta con
`uv run mister-assistant calibrate`. Recalcula cada fila de `player_gameweek` con
todas las combinaciones de las dos incógnitas de PLAN §2.1, elige la que más
acierta y clasifica las discrepancias.

## Reglas confirmadas con datos reales

1. **Puntos por fuente = base + bonus**. La base sale de la tabla de §2.1
   (cronistas: 4→14 … 0→−2; SofaScore por tramos de nota). El bonus de eventos
   se suma **en cada una de las cuatro fuentes**: gol según posición (6/5/4/3),
   gol de penalti +3 en cualquier posición, doble amarilla −3 y roja directa −6.
   Ejemplos: un delantero con gol tiene +3 en AS, Marca, MD y SofaScore; un
   defensa con gol de penalti, +3 (no +5).
2. **El bonus se suma también a una fuente S.C.** Caso decisivo (J4): un
   defensa con roja directa a los 6 minutos, con Marca en S.C., recibe −6 en
   Marca (0 de base − 6), y su Mixta es −8 = (−8 − 6 − 8 − 10) / 4. Sin bonus en
   la fuente S.C. saldría −7. Es el único caso de la muestra, pero no deja
   lugar a dudas porque se ven los puntos por fuente.
3. **0 estrellas ≠ S.C.** Mister da `rating = 0` (−2 puntos) a los suspensos y
   `null` (0 puntos) a los S.C.
4. **Media de las cuatro fuentes con S.C. = 0**, redondeada **al entero más
   cercano y con los empates hacia fuera del cero**: 7,25 → 7; −6,25 → −6;
   −2,5 → −3; 2,5 → 3. El redondeo bancario (el `round()` de Python) falla en
   302 de 2.188 filas, y el suelo o el truncado en más de 1.000.
5. **`points` (final) = `points_mix`** en todas las filas vistas: en esta liga
   no hay capitán ni otros multiplicadores.
6. **Eventos**: vienen de `/ajax/sw/gameweek`. Un gol de penalti llega solo como
   `penalty` (no también como `goal`), y una doble amarilla como `yellow` + `double`.

## Resultados

Datos: jornadas 1 a 7 de la temporada 26/27 (J6 sin su partido aplazado del 21
de octubre), cargados el 2026-10-08.

| Jornada | Filas | Aciertos |
|---|---|---|
| J1 | 320 | 320 |
| J2 | 315 | 315 |
| J3 | 314 | 314 |
| J4 | 319 | 319 |
| J5 | 316 | 316 |
| J6 (9 de 10 partidos) | 287 | 287 |
| J7 | 317 | 317 |
| **Total** | **2.188** | **2.188 (100%)** |

Contra `points_final` (los puntos que muestra la app): también 2.188 / 2.188.

| Hipótesis | Aciertos |
|---|---|
| **redondeo al más cercano con empates fuera del cero, bonus también en S.C.** | **2.188** |
| igual, sin bonus en S.C. | 2.187 |
| redondeo bancario | 1.886 |
| suelo | 1.161 |
| truncado | 1.148 |

- 191 filas tienen alguna fuente S.C., casi todas de suplentes con pocos minutos.
- Correcciones manuales de Mister (`has_manual_rating`): ninguna.
- **No hay discrepancias que explicar:** se cumple con holgura el criterio de
  aceptación de la Fase 1 (≥ 99%).

## Notas

- La **bonificación en dinero por goles** de la liga está desactivada
  (`prizes.goals = 0`); no tiene nada que ver con el bonus de puntos por
  gol, que sí existe.
