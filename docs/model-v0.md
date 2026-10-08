# Modelo v0 y once recomendado (Fase 3)

Código: `models/features.py` (entradas), `models/minutes.py`, `models/points.py`,
`decide/lineup.py` y `jobs/gameweek_report.py`. Versión: `v0.1`. Sin aprendizaje
automático: medias, regresión a la media y ajustes explícitos. Los parámetros de
la liga (`LeaguePriors`) se recalculan desde la BD en cada ejecución.

## Minutos (`minutes.py`)

- **P(titular)** = 0,75 × probabilidad de Fútbol Fantasy + 0,25 × su tasa de
  titularidades en los últimos 5 partidos de su equipo (suavizada). Sin Fútbol
  Fantasy: 0,6 × (0,85 si está en el once probable de Mister, si no 0,10) +
  0,4 × historial. Sin nada: solo el historial.
- **Baja:** probabilidad 0 en Fútbol Fantasy con código de lesión → no juega.
- **Sin partido** (su equipo no juega o tiene un aplazado) → no juega.
- **P(entra desde el banquillo | no es titular):** sus entradas en los partidos
  en que no fue titular, regresadas a 0,40 (defensas), 0,50 (medios y
  delanteros) o 0,02 (porteros).
- **Minutos esperados de titular:** su media de minutos como titular, regresada
  a la de su posición.

## Puntos (`points.py`)

Tres escenarios:

| Escenario | Puntos esperados |
|---|---|
| Titular | nivel propio + media de su posición según el resultado esperado + bonus de gol esperado + tarjetas |
| Entra desde el banquillo | media de la liga para suplentes de su posición (recoge los S.C. de las suplencias cortas, que cuentan 0) |
| No juega | 0 |

- **Resultado esperado:** con cuotas, P(victoria / empate / derrota) de su equipo
  sin margen; sin cuotas, el reparto medio de la liga. Es el ajuste que más
  pesa: un defensa titular saca de media 4,7 cuando su equipo gana y 1,9 cuando
  pierde (datos de las jornadas 1–7).
- **Nivel propio:** media de (Mixta sin bonus − media de su posición con el mismo
  resultado) en sus titularidades, regresada a 0 con 4 partidos ficticios.
- **Bonus de gol:** xG/90 (regresado a la media de su posición con 450 minutos
  ficticios) × minutos esperados / 90 × (goles esperados de su equipo según las
  cuotas / media de la liga) × puntos por gol de su posición.
- **Suelo y techo** (p20 y p80): 4.000 simulaciones por jugador (escenario,
  ruido normal con la desviación de su posición y goles Poisson), con semilla
  fija para que sean reproducibles.

## Once (`decide/lineup.py`)

El objetivo (suma de puntos esperados) es separable por jugador, así que para
cada una de las 7 formaciones gratuitas (las de pago no se pueden usar en esta
liga) basta con coger el mejor portero y los
mejores de cada línea. Eso es el óptimo exacto, sin programación entera (PuLP),
y lo comprueba un test contra la fuerza bruta. Nunca deja huecos si la
plantilla permite completar alguna formación; si no, el hueco cuesta −4.

**Dudas:** un titular con P(jugar) < 70% se marca y se calcula su recambio
(el mejor once sin él) y lo que se pierde con el cambio.

## Informe (`gameweek-report`)

- `--auto` (workflow `report.yml`, cada hora): solo en la **víspera**
  (20–28 h antes del primer partido) y en la **previa** (2–4 h antes), una vez
  por ventana (`report_log`). Antes recaptura alineaciones y cuotas.
- Guarda en `predictions` a **todos** los jugadores de la jornada (no solo los
  propios) y en `lineup_recommendations` el once y el actual. `before_kickoff`
  marca si se generó antes del primer partido: solo esos sirven para evaluar.
- Mensaje: once por líneas con puntos esperados, rango p20–p80 y P(jugar); dudas
  con su recambio; cambios respecto al once puesto en la app; banquillo.
- Manual: `gameweek-report` (envía ya) o `--dry-run` (solo imprime).

## Limitaciones conocidas de la v0

- Los goles de penalti se tratan como goles normales (+3 a +6 según posición en
  vez de +3 fijo).
- Los suplentes entran todos con la media de su posición, sin nivel propio.
- La correlación entre jugadores del mismo equipo no se modela: el rango del once
  completo no es la suma de los rangos.
- Calibración pendiente: se medirá en la Fase 5 con `predictions`.
