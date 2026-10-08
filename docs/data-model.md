# Modelo de datos (Fase 1)

Esquema: `supabase/migrations/20261008120000_initial.sql`. Postgres 16 (Supabase).

## Principios

- **Lo crudo primero.** Cada respuesta de Mister se guarda en `raw_responses`
  antes de normalizarla, sin `cfg.auth`, emails ni anuncios
  (`store/repo.py:sanitize`). Si un parser cambia, se puede renormalizar
  sin volver a pedir nada.
- **Snapshots append-only.** `manager_snapshot`, `squad_snapshot` y
  `market_snapshot` tienen una fila por entidad y día (`snapshot_date`, en hora
  de Madrid) y se insertan con `on conflict do nothing`: repetir el job el mismo
  día no cambia nada.
- **Dimensiones con el último valor.** `players`, `managers`, `teams`,
  `gameweeks` y `fixtures` se actualizan (upsert).
- **`player_gameweek` se actualiza** si se vuelve a pedir, porque Mister corrige
  notas a mano (`has_manual_rating`).
- **RLS activado** en todas las tablas y sin políticas: la API pública de
  Supabase no ve nada.

## Tablas

| Tabla | Clave | Alimentada por | Notas |
|---|---|---|---|
| `raw_responses` | (source, route, params_key, run_date) | todas las peticiones | `body_json` (rutas JSON) o `body_text` (HTML) |
| `job_runs` | id | cada job | estado `ok`/`partial`/`failed`, nº de peticiones, errores |
| `teams` | id | `/ajax/sw/gameweek` | |
| `players` | mister_player_id | users, gameweek, mercado | `slug` solo llega del mercado |
| `managers` | mister_manager_id | `/standings` + `/ajax/sw/users` | `is_me` = cuenta propia |
| `gameweeks` | id | `/ajax/sw/gameweek` | 38 jornadas con fechas y estado |
| `fixtures` | id | `/ajax/sw/gameweek` | la diaria guarda la jornada actual; el backfill, las cerradas |
| `manager_snapshot` | (manager, día) | `/ajax/sw/users` + `/ajax/balance` | `balance`, `max_bid` solo para el propio (los rivales están ocultos) |
| `squad_snapshot` | (player, día) | `/ajax/sw/users` | cláusula, valor, en venta, si estuvo en el último once |
| `market_snapshot` | (player, día) | `/market` | solo jugadores en venta ese día; `seller_manager_id` null = el juego |
| `league_events` | event_key | `/ajax/feed` | `transfer:<id_transfer>` y bajadas de cláusula |
| `player_gameweek` | (player, gameweek) | `/ajax/player-gameweek` + eventos de `/ajax/sw/gameweek` | notas por fuente, puntos por fuente, Mixta, final, goles, tarjetas, minutos |

## Robustez de los jobs

- Cada fuente o paso va en su propia transacción: si falla uno (HTML cambiado,
  404, error de datos), se registra en `job_runs.errors`, el job queda
  `partial` y el resto se guarda.
- Los errores de red con Mister se reintentan dos veces (a los 5 s y a los 20 s).
- Si se pierde la conexión con Postgres, el job se aborta como `failed` y deja de
  pedir a Mister (no tendría dónde guardar).
- El backfill es reanudable: salta los `player_gameweek` ya guardados. Procesa
  jornadas `finished` y `ongoing`, pero solo los partidos ya puntuados; un
  aplazado se recoge en la ejecución diaria cuando se juegue.

## Huecos conocidos

- **Valor de mercado de los jugadores libres que no están en venta:** no hay
  una ruta barata para pedirlo. Hoy se guarda el de los jugadores con dueño
  (`squad_snapshot`) y el de los que están en venta (`market_snapshot`).
  `/ajax/sw/players` da la serie diaria de un año por jugador, útil para un
  backfill de valores en la Fase 4.
- **Precio de compra:** no viene en `/ajax/sw/users`; `clause_floor` es la base
  de la cláusula (máx. entre compra y valor). El precio exacto está en el feed
  (`league_events.price`) y en `/ajax/sw/players` (`transfer.price`).
- **Saldos rivales:** ocultos en la liga (`show_balances = 0`). Habrá que
  estimarlos a partir de `league_events` y de los premios por jornada (Fase 4).
