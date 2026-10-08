# Fuentes externas (Fase 2)

| Fuente | Qué aporta | Cómo | Frecuencia | Estado |
|---|---|---|---|---|
| **Mister** (`preview` de la jornada) | Once probable de Mister y si está confirmado | `/ajax/sw/gameweek` | con cada captura de alineaciones | ✅ |
| **Fútbol Fantasy** | Once probable y suplentes con % de titularidad; código de lesión | HTML: índice + ficha de cada partido | 7 capturas por semana (`lineups.yml`) | ✅ |
| **SofaScore vía Mister** | xG, xA, tiros, tiros a puerta, pases clave, toques, minutos | `stats` de `/ajax/player-gameweek`, ya guardado | con el backfill (sin peticiones nuevas) | ✅ sustituye a Understat |
| **The Odds API** | 1X2 y más/menos goles (consenso sin margen) | API JSON, mercados `h2h,totals`, región `eu` | 1 al día (2 créditos; 500/mes) | ✅ falta `ODDS_API_KEY` |
| ~~Understat~~ | — | — | — | ❌ descartado |

## Decisiones

- **Understat, descartado.** Su `robots.txt` prohíbe todo (`Disallow: /`). Además
  no hace falta: Mister ya da las estadísticas de SofaScore de cada jugador en
  cada partido. SofaScore omite las claves que valen 0: en las 2.188 filas
  cargadas, `expectedGoals` falta exactamente cuando `totalShots` = 0, así que
  xG = 0 en ese caso. El xG total (205,9) cuadra con los goles reales (200).
- **Cuotas de goleador:** The Odds API no las ofrece para fútbol. P(gol) saldrá
  del xG por 90 minutos y de los minutos esperados (Fase 3), corregido con el
  total de goles esperado del partido (`totals`).
- **Lo crudo de Fútbol Fantasy:** la ficha de un partido pesa ~2 MB, casi todo
  maquetación. Se guardan los atributos de cada jugador (`raw_players`, ~60 KB
  en JSON), que son toda su información sobre ellos. Así el plan gratuito de
  Supabase aguanta años.
- **Histórico de temporadas anteriores:** las fichas de jugador de Fútbol
  Fantasy (`/jugadores/<slug>/laliga-25-26`) traen una tabla por jornada con
  picas, estrellas y nota de SofaScore. Es la única fuente vista para entrenar
  con temporadas pasadas, pero cuesta una petición de ~1,7 MB por jugador y
  temporada (~500 por temporada). **No se ha implementado**; se decidirá en la
  Fase 5, cuando haya que entrenar, con una carga única y lenta.

## Fútbol Fantasy

- `robots.txt`: `Disallow:` vacío (se permite todo). Aun así, 5–7 s entre
  peticiones y 11 por captura.
- Índice `/laliga/posibles-alineaciones`: `section.proxjornada` con los 10
  partidos de la jornada, nombres de equipo (`img.escudo[alt]`) e id de cada
  escudo, que es la clave de `team_xref`.
- Ficha `/partidos/<id>-<local>-<visitante>`: cada jugador es un
  `div.jugador_<id>` con `a[data-probabilidad]` («70%»), `data-lesion` (`-1` =
  disponible; `0`, `2` y otros = lesión o duda; significado exacto por
  confirmar), `data-onceff` (`titular` o `suplente`) y el nombre en el `alt` de la
  foto. Algunos no tienen enlace a su ficha (`href="#"`), así que el *slug* puede
  faltar.
- Cada partido de Fútbol Fantasy se asigna al fixture de Mister con esos dos
  equipos que se juegue en los próximos 10 días, sea de la jornada que sea
  (los aplazados vienen de otra jornada).

## Identidades

- **Equipos** (`team_xref`): alias explícitos para los nombres que el difuso
  confunde («Atletico Madrid» frente a «Real Madrid») y, si no, el mejor por
  nombre siempre que destaque sobre el segundo. Los 20 equipos se emparejan de
  forma única en Fútbol Fantasy y en The Odds API (tests).
- **Jugadores** (`player_xref`): solo entre los jugadores de Mister del mismo
  equipo. Se acepta solo:
  1. si el nombre normalizado (sin tildes) coincide exactamente (`exact`), o
  2. si la puntuación difusa es ≥ 90 y saca ≥ 6 puntos al segundo candidato
     (`fuzzy`). Cuentan como 95 «un nombre contiene al otro» («Natan» ⊂ «Natan
     Souza») y «mismos tokens con diminutivos» («Fede» = «Federico»).
  Lo demás va a `identity_review` con sus tres mejores candidatos. Un
  emparejado verificado a mano no lo pisa nunca el automático.
- **Revisión manual:**
  `mister-assistant identity export-review docs/raw/identity-review.csv` →
  rellenar la columna `decision` con un `player_id` de Mister o `ignorar`
  (`sugerencia` es solo una ayuda: no se aplica si no se copia a `decision`) →
  `mister-assistant identity import-review docs/raw/identity-review.csv`.
  `identity rematch` reintenta la cola cuando Mister tiene jugadores nuevos.
  Para encontrar el `player_id`: la columna `candidates` del CSV,
  `mister-assistant identity search NOMBRE [--team EQUIPO]`, o la URL de la ficha
  en Mister (`/players/<id>/<slug>`).

## Cobertura (criterio de la Fase 2)

Medido el 2026-10-08, tras la primera captura (`identity coverage`):

| Universo | Emparejados |
|---|---|
| Jugadores con minutos esta temporada | 440 / 460 (95,7%) |
| Jugadores con minutos en las últimas 3 jornadas (`--recent 3`) | **390 / 395 (98,7%)** |

**Criterio aceptado (2026-10-08): jugadores con minutos en las últimas 3
jornadas** (`identity coverage --recent 3` ≥ 98%). Los que dejaron de jugar no
salen en las alineaciones probables y no aportan nada al modelo.

Los 20 que faltan en la temporada no aparecen en las fichas de partido de la
J8 de Fútbol Fantasy: casi todos dejaron de jugar tras las primeras jornadas
(lesiones largas, salidas, canteranos puntuales). Solo uno tiene su
equivalente en Fútbol Fantasy con otro nombre: «Jonny Castro» = «Jonny Otto»
(revisión manual). Irán entrando según vuelvan a aparecer en las
convocatorias.
