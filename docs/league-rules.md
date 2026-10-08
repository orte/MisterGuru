# Normas de la liga

**Fuente de verdad: la configuración de Mister** (`/ajax/sw/admin`, leída el
2026-10-08). Si las notas manuales y Mister discrepan, manda Mister; las
correcciones aplicadas están al final. Lo que Mister codifica y aún no sabemos
interpretar se marca *(por confirmar)*.

- Liga: Urtarrillak 25 afaria (id 1277524), privada.
- Participantes: **14** (clasificación). Máximo permitido: 20.
- Saldo inicial: 50.000.000 €. Jugadores iniciales por plantilla: 15.
- Sistema de puntuación: Mixto (AS, Marca, Mundo Deportivo y SofaScore; 25% cada uno).
- Mercado: dos ciclos al día *(según las notas; Mister lo guarda como
  `market_speed = 1`, código por confirmar con las horas de `data-ends`)*. Cada
  jugador dura un ciclo en el mercado; el juego saca 20 jugadores por ciclo.
- Jugadores en venta simultáneos por miembro: 8.
- Máximo de jugadores por plantilla: 40.
- Salarios: no. Cesiones: no.
- Cláusulas: sí. Permitidas durante la jornada, sin bloqueo ni límite diario,
  sin plazo tras fichar y sin límite de clausulazos recibidos *(interpretación
  de los códigos por confirmar)*.
- Deuda máxima: saldo actual + 25% del valor de mercado del equipo. Mister la da
  ya calculada en `/ajax/balance` (`maxDebt`).
- Bonificaciones:
  - 100.000 € por punto conseguido en la jornada.
  - Por puesto en la clasificación de la jornada, a la inversa:
    **1º → 0 €**, 2º → 200.000 €, y +200.000 € por cada puesto hasta
    15º → 2.800.000 €. Con 14 participantes, el último (14º) cobra 2.600.000 €.
    Fórmula: (puesto − 1) × 200.000 €.
- Bonificación por goles: no.
- Capitanes: no.
- Cambios durante la jornada: no.
- Saldos de los rivales: **ocultos**. Hay que estimarlos para las cláusulas (Fase 4).
- Formaciones válidas: 4-4-2, 4-5-1, 4-3-3, 3-4-3, 3-5-2, 5-4-1, 5-3-2, 4-2-4,
  4-6-0, 3-3-4, 3-6-1, 5-5-0.

## Correspondencia con la configuración de Mister

| Campo | Valor |
|---|---|
| `provider` | `mix` |
| `max_users` | 20 |
| `startingBalance` / `startingPlayers` | 50.000.000 / 15 |
| `market_speed` / `market_stay` / `market_players` | 1 / 1 / 20 |
| `sale_limit` | 8 |
| `team_limit` | 40 |
| `salaries` / `loans` | 0 / 0 |
| `clauses` | 1 |
| `clauses_gameweek` / `clauses_block` / `clauses_daily` | 1 / 0 / 0 |
| `transfer_wait` / `max_inbound_clauses` | 0 / 0 |
| `max_debt` | 4 (código de «saldo + 25% del valor de equipo») |
| `prizes.points` | 100.000 |
| `prizes.positions` | `{2: 200.000, 3: 400.000, …, 15: 2.800.000}` (no hay entrada para el 1º) |
| `prizes.goals` | 0 |
| `is_captain_enabled` | 0 |
| `live_changes` | 0 |
| `show_balances` | 0 |
| `sport.parameters.formations` | las 12 de arriba |

## Correcciones respecto a las notas originales

- **Premio por puesto:** las notas decían «200.000 € para el 1º y 2.800.000 € al
  15º». Según Mister, el 1º cobra 0 € y el escalado empieza en el 2º.
- **«Total de jugadores: 15»:** son los jugadores iniciales por plantilla
  (`startingPlayers`), no los participantes, que son 14.
