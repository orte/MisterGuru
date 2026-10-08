# Evaluación y modelo v1 (Fase 5)

Código: `evals/backtest.py`, `evals/weekly.py` y `models/points_v1.py`.
Comandos: `mister-assistant backtest [--save]` (reproducible, sin red) y
`mister-assistant weekly-eval [--dry-run]` (workflow `weekly.yml`, martes 09:30 UTC).

## Backtest «como si»

Hasta la J8 no hay ninguna jornada cerrada con predicciones guardadas, así que
cada jornada pasada se predice con lo que se sabía antes de ella: historial,
xG y parámetros de la liga solo de jornadas anteriores. **Sin Fútbol Fantasy ni
cuotas**, porque entonces no se capturaban; en producción v0 los usa, así que
el backtest le es desfavorable. v1 se entrena en cada jornada solo con las
anteriores.

### Resultado (2026-10-08, jornadas 3 a 7)

| Jornada | Modelo | Error medio | Sesgo | Spearman | Once recomendado / puesto / los 11 más caros |
|---|---|---|---|---|---|
| J3 | v0 | 2,04 | +0,01 | 0,53 | 34 / 15 / 27 |
| J4 | v0 | 2,12 | +0,11 | 0,39 | 41 / 35 / 44 |
| J4 | v1 | 2,12 | +0,05 | 0,37 | 31 / 35 / 44 |
| J5 | v0 | 1,88 | +0,00 | 0,47 | 31 / 30 / 29 |
| J5 | v1 | 1,84 | −0,13 | 0,48 | 31 / 30 / 29 |
| J6 | v0 | 2,17 | +0,20 | 0,34 | 42 / 34 / 47 |
| J6 | v1 | 2,14 | +0,07 | 0,35 | 47 / 34 / 47 |
| J7 | v0 | 2,03 | −0,02 | 0,47 | 39 / 34 / 41 |
| J7 | v1 | 1,99 | −0,04 | 0,49 | 39 / 34 / 41 |

(483 jugadores por jornada: todos los de los equipos que jugaron.)

- **¿v1 mejora a v0?** En el error, sí, pero muy poco: −0,029 puntos por
  jugador (un 1,4 %), con un intervalo de confianza del 95 % de [−0,048, −0,011]
  por *bootstrap* pareado. En el once recomendado, no: 148 frente a 153 puntos
  reales en J4–J7. **Se mantiene v0.** La regla: v1 solo sustituye a v0 si
  todo el intervalo del error queda por debajo de 0 **y** no elige peores onces.
- **¿El once recomendado bate al alineado?** Sí: en J3–J7 el de v0 suma **187
  puntos reales frente a 148** con la misma plantilla (+7,8 por jornada). Pero
  «los 11 más caros» suman **188**: con esta información (sin Fútbol Fantasy
  ni cuotas) el modelo no aporta más que alinear a los más valiosos. La
  evaluación de las jornadas con predicción real (desde la J8) dirá si las
  fuentes externas marcan la diferencia.
- Los puntos del once puesto reconstruidos coinciden exactamente con los de
  Mister (J3 15, J4 35, J5 30, J6 34, J7 34): la comparación es fiable.

### Calibración de P(jugar) (v0)

| Predicha | Observada | Jugadores |
|---|---|---|
| 0,14 | 0,13 | 82 |
| 0,31 | 0,35 | 399 |
| 0,49 | 0,49 | 348 |
| 0,71 | 0,63 | 550 |
| 0,90 | 0,85 | 1.036 |

Bien calibrada por abajo; algo optimista por arriba: del 63 % de los que se dan
como titulares probables sin Fútbol Fantasy, una parte no juega.

## Modelo v1 (`models/points_v1.py`)

Sustituye solo la base del titular (la Mixta sin bonus cuando es titular); el
resto (minutos, bonus de gol, simulación) es el de v0.

- **AS, Marca y MD**: modelo ordinal (Frank y Hall) con LightGBM sobre los
  niveles ordenados por puntos: 0 estrellas (−2) < S.C. (0) < 1 (+2) < 2 (+6)
  < 3 (+10) < 4 (+14). Un clasificador binario por umbral.
- **SofaScore**: regresión de la nota. Los residuos del entrenamiento convierten
  la nota esperada en puntos con la tabla oficial (que no es lineal).
- **Entradas** (solo lo anterior a la jornada): medias recientes de cada fuente
  como titular, titularidades, tasa de titularidad, posición, log del valor de
  mercado, forma del equipo y del rival, si juega en casa y xG por 90 minutos.
- Árboles pequeños (7 hojas, 25 filas mínimas por hoja, 150 rondas) por los
  pocos datos: unos 220 titulares por jornada.

## Evaluación semanal (`weekly-eval`)

Cada martes, sin peticiones:

1. **Jornada**: la última cerrada con predicción guardada antes del primer
   partido. Error, sesgo, Spearman y el once **enviado** frente al puesto y a
   los 11 más caros.
2. **Mercado**: rellena `outcome` de las recomendaciones con al menos 7 días
   (variación de valor y puntos desde entonces) y compara los fichajes
   recomendados con el resto del mercado de ese día.
3. **Backtest** v0 frente a v1 con todas las jornadas disponibles.

Todo queda en `evaluations` y se resume por Telegram.
