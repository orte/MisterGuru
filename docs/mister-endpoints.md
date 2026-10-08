# Catálogo de endpoints de Mister

Descubierto a partir de `docs/mister.har` (captura del 2026-10-08, jornada 8
sin empezar, temporada 26/27). Todo lo de aquí es **de consulta**; las rutas que
modifican el juego no se catalogan ni se implementan (PLAN.md §7).

La lista blanca del cliente está en `src/mister_assistant/sources/mister.py`
(`ROUTES`); un test comprueba que cada ruta de la lista aparece en este documento.

## Autenticación y sesión

| Elemento | Dónde va | Variable `.env` | Notas |
|---|---|---|---|
| `token` | Cookie | `MISTER_TOKEN` | JWT de ~5 min; el servidor lo reemite (ver abajo). `doctor` muestra su `exp` sin verificar la firma. |
| `x-auth` | Cabecera en **todas** las peticiones | `MISTER_X_AUTH` | Las respuestas con `cfg` (p. ej. `/ajax/feed`) traen el valor vigente en `cfg.auth`; el cliente lo adopta en memoria. |
| `PHPSESSID` | Cookie | `MISTER_PHPSESSID` | Sesión PHP. |
| `refresh-token` | Cookie | `MISTER_REFRESH_TOKEN` | Opcional; con ella el servidor reemite `token`. |
| `authenticated=true` | Cookie | — | La web la envía; el cliente también. |

Otras cabeceras que manda la web y replica el cliente: `x-requested-with:
XMLHttpRequest`, `origin`, `referer`, `content-type:
application/x-www-form-urlencoded`. Las páginas HTML parciales llevan además
`partial-request: true`.

**Todas las rutas son POST**, incluso las de consulta (los parámetros van como
formulario). Por eso la seguridad del cliente es una lista blanca de rutas y
parámetros, no el método HTTP.

Las respuestas JSON tienen la forma `{"status": "ok", "data": …}`; algunas
añaden `cfg` (configuración global de la web, incluido `cfg.auth`).

### Detección de sesión caducada

El HAR solo contiene respuestas 200, así que la forma exacta del error es
**desconocida**. El cliente considera sesión caducada: HTTP 401/403/419/440,
redirección a login/onboarding/raíz, HTML de login en lugar de JSON, o
`status != "ok"` con mensaje que mencione sesión/login/auth/token. Al ver el
primer fallo real, ajustar `_check_status`/`_parse_json` con la respuesta grabada.

### Renovación de la sesión (investigación Fase 0)

Comprobado en vivo el 2026-10-08 con `doctor` y peticiones de solo lectura a
`/ajax/balance` (sin imprimir ningún secreto):

1. **El JWT `token` vive unos 5 minutos** (claims: `alg`, `exp` como cadena
   numérica, `userid`). El de `.env` estaba vencido (exp 08:56 UTC) cuando se
   probó (09:09 UTC).
2. **Con el token vencido, Mister sigue respondiendo `ok`**: la autenticación
   efectiva de `/ajax/*` descansa en `PHPSESSID` y/o `x-auth`, no en el exp del JWT.
3. **Si se envía la cookie `refresh-token`, el servidor reemite `token`** vía
   `Set-Cookie` con un exp nuevo a +5 min. Sin `refresh-token`, la petición
   también funciona, pero no se reemite el token.
4. `x-auth` viaja también en `cfg.auth` de las respuestas con `cfg`; en la
   prueba no cambió (`x-auth rotado: no`).
5. El HAR no contiene ninguna llamada de refresh explícita (y Chrome exporta el
   HAR sin cookies), así que no existe un endpoint de renovación conocido:
   la renovación es implícita en cada petición.

**Conclusión:** la sesión se mantiene sola mientras `PHPSESSID`/`x-auth`
(y el `refresh-token`) sigan siendo válidos; el cliente adopta en memoria el
`token` y el `x-auth` que reemite el servidor y no persiste nada.

**Pendiente (no se puede saber con una sola captura):** cuánto duran
`PHPSESSID`, `x-auth` y `refresh-token`. Método: ejecutar `doctor` a diario
(Fase 1 lo hará el job programado) y anotar el día en que devuelve código 3
(sesión caducada). Con ese dato se decide §8 «¿Dónde corre?»: si dura semanas,
GitHub Actions con aviso por Telegram para renovar a mano; si dura horas o días,
máquina propia.

## Identificadores

| Id | Ejemplo | Qué es |
|---|---|---|
| `id_community` | 1277524 | Liga. La cuenta pertenece a varias (`cfg.context.communities`); la activa es `cfg.context.community`. |
| `id_uc` / id de usuario | 15448471 | Participante **dentro de la liga** (usado en `/ajax/sw/users`, `id_owner`, `owner`). Es la clave de `managers`. |
| `id_user` | 892275 | Cuenta global de Mister (aparece en el feed). |
| `slug` | `jon-orte` | Acompaña a `id` en usuarios y jugadores; sale de los enlaces `users/{id}/{slug}` y `players/{id}/{slug}`. |
| `id_player` | 53111 | Jugador (estable entre temporadas). Clave de `players`. |
| `id_team` | 4 | Club de LaLiga; nombres en `cfg.teams`. |
| `id_gameweek` | 4049 | Jornada (id interno); `number`/`gameweek` es el número 1–38. |
| `id_match` | 38001 | Partido. Clave de `fixtures`. Trae `id_sofa` (id de SofaScore). |

`position`: 1 portero, 2 defensa, 3 centrocampista, 4 delantero.

## Endpoints JSON

### `/ajax/feed` — feed de la liga

- **Parámetros:** `offset` (0, 20, …), `cardsPerPage` (20), `end=false`, `loading=true`.
- **Respuesta:** `data[]` de tarjetas + `cfg`.
  - Tarjeta: `id`, `id_community`, `category`, `created` (fecha absoluta),
    `date` (relativa), `data[]`.
  - Categorías vistas: `transfer` (fichajes, cláusulas, ventas: `id_transfer`,
    `id_uc_from`, `id_uc_to`, `from`, `to`, `type` [`clause`, …], `price`,
    jugador con `value`, `points`, `streak`, y `bids`), `clauses_drops`
    (bajadas de cláusula: `floor`, `multiplier`, `old_multiplier`, `user`),
    `porra`, `post`.
  - `cfg.context.communities[]`, `cfg.context.community.id`,
    `cfg.context.season`, `cfg.market_date`, `cfg.teams` (clubes), `cfg.auth`.
- **Alimenta:** `league_events` (pujas ganadas, clausulazos, precios),
  `managers`. `doctor` lo usa para validar la sesión y listar ligas.

### `/ajax/balance` — saldo propio

- **Parámetros:** ninguno.
- **Respuesta:** `data.current`, `data.future` (tras operaciones pendientes),
  `data.maxDebt` (puja máxima posible = saldo + deuda permitida).
- **Alimenta:** `manager_snapshot` (solo el propio).
- ⚠️ **Saldos rivales no expuestos**: la liga tiene `show_balances = 0`. Habrá que
  estimarlos (saldo inicial + premios por jornada + ventas − compras del feed)
  para las cláusulas de Fase 4.

### `/ajax/sw/users` — participante y su plantilla (propia o rival)

- **Parámetros:** `post=users`, `id` (id_uc), `slug`, `comments=0`.
- **Respuesta (`data`):**
  - `team_now[]`: plantilla completa con `id`, `name`, `position`, `id_team`,
    `points`, `avg`, `status`, `streak[5]`, `value`, `prev_value`, `price`
    (si está en venta), `transfer_origin`, y `clause` {`value` (cláusula
    actual), `floor`, `multiplier`, `percentage`, `shield`, `tier`}.
  - `value` {`value`, `prev_value`}: valor de equipo.
  - `userInfo` {`name`, `avatar`, `id_community`}.
  - `season` {`rank`, `points`, `avg`}.
  - `gameWeeks[]` + `userGameWeeks{id_gameweek: {points, formation}}`:
    puntos por jornada del participante.
  - `lineup.positions{pos: {slot: jugador}}` + `bench[]`: once de la última
    jornada, con `points`, `played`, `match_status` y fechas `*_graded_date`.
- **Alimenta:** `squad_snapshot` (dueño, cláusula, alineado), `manager_snapshot`
  (valor, puntos, posición), `managers`.
- Con el `id` propio devuelve la plantilla propia (mejor que parsear `/team`).

### `/ajax/sw/players` — ficha de jugador

- **Parámetros:** `post=players`, `id` (id_player), `slug`, `comments=0`.
- **Respuesta (`data`):**
  - `player`: `value`, `previousValue`, `points`, `avg`, `status`, `injury[]`,
    `team`, `owner` (id_uc, nombre), `transfer` {`date`, `origin`, `price`
    (precio de compra)}, `clause`, `market` (en venta, `price`), `bio`,
    `clausesRanking`.
  - `points[38]`: por jornada `id`, `number`, `points.points`, `events`
    (p. ej. `saved_penalty`), `injury`, `teamPlayed`, `rivalId`.
  - `points_history[]`: totales de temporadas anteriores.
  - `values[]` (variación 1 día/semana/mes/año) y `values_chart.points[]`
    (**serie diaria del último año** de valor de mercado).
  - `next_match`, `home`/`away` (puntos en casa/fuera), `starter`, `owners[]`
    (historial de traspasos en la liga con precio y tipo).
- **Alimenta:** `market_snapshot` (backfill de valores con `values_chart`),
  `players`, `league_events` (vía `owners`).

### `/ajax/player-gameweek` — desglose de puntos por fuente y jornada ✅

- **Parámetros:** `id_manager` (id_uc de quien consulta), `id_gameweek`, `id_player`.
- **Respuesta (`data`):**
  - Partido: `id_match`, `id_home`, `id_away`, `goals_home`, `goals_away`,
    `status`, `match_team_id`.
  - Por fuente: `rating_as`/`points_as` (picas), `rating_marca`/`points_marca`
    y `rating_md`/`points_md` (estrellas), `rating_ss`/`points_mr`
    (**SofaScore = clave `mr`**), `points_mix` (Mixta, la de esta liga),
    `points_mix2`, `points_marca_stats`; cada una con su `*_graded_date`.
  - `providers{as, marca, mr, md}`: `rating`, `points`, `base` (puntos antes de
    extras) y `mixValue` (peso: `mix` 25% cada una; `mix2` da 50% a SofaScore).
    `mainProvider` = `mix` con `parent: [as, marca, mr, md]`.
  - `stats` (JSON en texto, SofaScore): `minutesPlayed`, `expectedGoals`,
    `expectedAssists`, tiros, pases, duelos…
  - `marca_stats_rating_detailed_filtered` (JSON en texto): `minutesPlayed`,
    `goals`, `goalAssist`, `ownGoals`, `penaltyMiss`, `penaltySave`,
    `penaltyWon`, `yellowCard`, `doubleYellowCard`, `redCard`, `saves`…
    → aquí están goles, penaltis y tarjetas para el motor de puntuación.
  - `*_manual` y `latest_provider_rating_*`: correcciones manuales de Mister.
  - `points` (final), `value` (valor del jugador en esa jornada).
- **Alimenta:** `player_gameweek` (picas AS, estrellas Marca/MD, nota
  SofaScore, minutos, goles, penaltis, tarjetas, puntos Mister) y `match_stats`.
- **Conclusión de aceptación:** Mister **sí expone el desglose por fuente**; no
  hace falta la alternativa de Jornada Perfecta. Coste: una petición por
  jugador y jornada; para el backfill de Fase 1 hay que limitar el universo
  (jugadores con minutos) y espaciar.

### `/ajax/sw/gameweek` — jornada y calendario

- **Parámetros:** `post=gameweek`, `id` (id_gameweek), `comments=0`.
- **Respuesta (`data`):**
  - `gameweeks[38]`: `id`, `gameweek` (número), `status` (`finished`, …),
    `firstMatchDate`, `lastMatchDate` → **calendario de jornadas** y hora límite
    para fijar el once.
  - `gameweekStatus`: jornada pedida, `status` (`unstarted`, …),
    `secondsRemainingToStart`.
  - `games[]`: partidos con `id`, `id_home`, `id_away`, `home`, `away`,
    `goals_*`, `status` (`fixture`, `played`), `date.ts`, `id_sofa`,
    `confirmed_lineups`, fechas `*_graded_date`.
  - `preview{id_match: {players{id_team: [...]}, confirmed}}`: **alineaciones
    probables de Mister** por partido (`confirmed` 0/1).
  - `gameweek_user` {`points`, `rank`, `negative`} y `lineup`/`bench` propios
    para esa jornada.
- **Alimenta:** `fixtures`, `lineup_forecast` (fuente `mister`).

### `/ajax/sw/admin` — configuración de la liga

- **Parámetros:** `post=admin`.
- **Respuesta:** `data.community` (≈ 85 campos) y `data.sport.parameters`
  (`formations[12]`, `defaultBalance`, `players: 11`). Campos útiles:
  `name`, `provider` (`mix`), `team_limit`, `sale_limit`, `market_speed`,
  `market_stay`, `clauses`, `clauses_gameweek`, `clauses_block`,
  `transfer_wait`, `loans`, `salaries`, `max_debt`, `show_balances`,
  `is_captain_enabled`, `live_changes`, `prizes` {`points`, `goals`,
  `positions{puesto: €}`}, `startingBalance`, `startingPlayers`, `max_users`.
- **Alimenta:** restricciones del optimizador; contrastado con
  `docs/league-rules.md`. Es la sección de administración: puede fallar si la
  cuenta no es admin; `doctor` lo tolera.

## Páginas HTML parciales

Se piden con POST sin cuerpo y `partial-request: true`; devuelven
`<div id="partial-wrapper"><div id="partial-content" data-pag="…">`. Solo se
usan cuando no hay JSON equivalente. El parser irá aislado y con error tipado
(PLAN §7).

### `/market` — mercado del día

- `ul#list-on-sale > li` (≈70) con `data-position`, `data-price` (precio de
  venta), `data-owner` (id_uc; vacío = mercado del juego), `data-ends`
  (timestamp de fin del ciclo), `data-loanable`.
- Dentro: enlace `players/{id}/{slug}`, `.points`, `.underName` (valor de
  mercado), `.value-arrow` (subida/bajada), `.avg`, `.streak span` (últimas 5),
  equipo y rival en los logos.
- Pujas propias: `button.btn-bid[data-id_player]` (sin importe ajeno).
- **Alimenta:** `market_snapshot` (en venta, precio, vendedor, fin).

### `/team` — plantilla propia y once

- `li#player-{id}` por jugador (mismos campos que el mercado) y huecos
  `#slot-1…#slot-11` del once; `_FG_data.gameWeekId`, `_FG_data.formations`.
- Preferir `/ajax/sw/users` con el id propio; esta página queda como respaldo.

### `/standings` — clasificación

- `.panel-total li` y `.panel-gameweek`: posición, enlace `users/{id}/{slug}`,
  nombre, «N jugadores · € valor», puntos y diferencia.
- **Alimenta:** `managers` (ids y slugs para `/ajax/sw/users`) y
  `manager_snapshot`.

### `/feed`

- Versión HTML del feed. Se usa `/ajax/feed` en su lugar.

## Cobertura del criterio de aceptación de Fase 0

| Necesidad | Endpoint |
|---|---|
| Plantilla propia | `/ajax/sw/users` (id propio) · respaldo `/team` |
| Plantillas rivales | `/ajax/sw/users` (ids de `/standings`) |
| Mercado | `/market` (HTML) · valores por jugador en `/ajax/sw/players` |
| Saldos | `/ajax/balance` (propio). Rivales: ocultos en esta liga → estimar |
| Feed | `/ajax/feed` |
| Calendario | `/ajax/sw/gameweek` (`gameweeks[]` y `games[]`) |
| Desglose de puntos por fuente y jornada | `/ajax/player-gameweek` |

## No catalogado (a propósito o por falta de captura)

- Rutas de escritura (pujar, vender, alinear, cláusulas, cambiar de liga con
  `/action/change`): fuera de alcance hasta la Fase 7.
- Plantillas `/views/**/*.twig`: son las vistas que la web renderiza en cliente;
  útiles para entender campos, no para pedir datos.
- Listado de jugadores con filtros (`/ajax/sw/players` con `offset`/`filters`,
  usado por `elexrallen/Mister`) y `/ajax/player-community-info`: no aparecen en
  este HAR; añadirlos a la lista blanca solo tras verlos en una captura.
