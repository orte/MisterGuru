# Mister Assistant

Asistente personal (solo lectura) para un equipo de Mister Fantasy. El plan
completo está en [PLAN.md](PLAN.md).

## Estado

**Fase 5 — evaluación y modelo entrenado.**

- Fase 0: cliente de Mister de solo lectura con lista blanca de rutas
  (`sources/mister.py`), [catálogo de endpoints](docs/mister-endpoints.md),
  [reglas de la liga](docs/league-rules.md) y `doctor`.
- Almacén Postgres (Supabase) con migraciones en `supabase/migrations/`
  ([modelo de datos](docs/data-model.md)).
- `snapshot-daily`: foto diaria de plantillas, saldo, mercado, clasificación y feed.
- `backfill-gameweeks`: desglose por fuente de las jornadas disputadas.
- `scoring/mixed.py`: motor de puntuación Mixta y
  [calibración](docs/scoring-calibration.md) contra Mister.
- Workflow diario en GitHub Actions con aviso por Telegram.
- Fase 2 ([fuentes externas](docs/external-sources.md)): alineaciones
  probables de Mister y Fútbol Fantasy, cuotas (The Odds API), xG/xA/tiros por
  partido (SofaScore vía Mister) y emparejado de jugadores con cola de revisión.
- Fase 3 ([modelo v0](docs/model-v0.md)): minutos y puntos esperados por
  jugador, once óptimo con dudas y recambios, e informe por Telegram la víspera
  y 3 h antes del primer partido (`report.yml`).
- Fase 4 ([mercado](docs/market.md)): serie diaria de valores, modelo de
  variación de valor, fichajes y pujas, cláusulas ofensivas y defensivas,
  ventas e informe matinal de mercado.
- Fase 5 ([evaluación](docs/evaluation.md)): backtest «como si» de v0 y v1
  (LightGBM), evaluación semanal de lo predicho y recomendado, y auditoría del
  mercado (`weekly.yml`).

## Puesta en marcha

Requiere Python 3.12 y [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # rellenar con los valores de sesión de DevTools
uv run mister-assistant doctor
```

`doctor` valida las credenciales, avisa de cuándo caduca el token, muestra la
liga activa, las demás ligas de la cuenta y el saldo. Nunca imprime secretos.
Códigos de salida: `0` OK, `1` error, `2` faltan variables, `3` sesión caducada.

### Comandos

| Comando | Qué hace |
|---|---|
| `doctor [--notify]` | Valida la sesión de Mister y lista las ligas |
| `migrate` | Aplica las migraciones SQL pendientes en `DATABASE_URL` |
| `snapshot-daily [--force]` | Foto del día (~20 peticiones). Idempotente: una vez por día |
| `backfill-gameweeks [--gameweek N] [--max-requests N]` | Desglose de jornadas cerradas (~300 peticiones por jornada). Reanudable |
| `calibrate [--target points_mix\|points_final]` | Recalcula `player_gameweek` con el motor y lo compara con Mister |
| `notify TEXTO` | Envía un mensaje por Telegram |
| `capture-lineups [--skip-mister]` | Alineaciones probables de Mister y Fútbol Fantasy (12 peticiones) |
| `capture-odds` | Cuotas 1X2 y goles (1 petición, 2 créditos). Sin `ODDS_API_KEY` no hace nada |
| `derive-match-stats` | Rellena `match_stats` desde lo crudo (sin red) |
| `backfill-values [--max-requests N]` | Serie de valor de un año e historial de traspasos por jugador (una vez; ~550 peticiones) |
| `backfill-feed [--max-pages N]` | Feed de la liga hasta donde lo sirve Mister (~3 semanas) |
| `backtest [--save]` | Backtest de v0 frente a v1 y once recomendado frente al puesto (sin red) |
| `weekly-eval [--dry-run]` | Evaluación semanal de predicciones y recomendaciones, por Telegram |
| `market-report [--dry-run] [--force]` | Informe de mercado y cláusulas (sin peticiones) |
| `gameweek-report [--auto] [--dry-run] [--no-refresh]` | Predicciones, once recomendado e informe por Telegram |
| `identity coverage [--recent N]` | Cobertura del emparejado con Fútbol Fantasy |
| `identity rematch` | Reintenta la cola de revisión (sin red) |
| `identity export-review CSV` / `import-review CSV` | Revisión manual de emparejados dudosos |

Todos se ejecutan con `uv run mister-assistant <comando>`.

Primera puesta en marcha de la Fase 1:

```bash
uv run mister-assistant migrate
uv run mister-assistant snapshot-daily
uv run mister-assistant backfill-gameweeks      # ~2 h para 7 jornadas; se puede cortar y reanudar
uv run mister-assistant calibrate
```

### Base de datos

`DATABASE_URL` es la cadena de conexión de **Postgres**, no la URL https de la
API de Supabase. En Supabase: **Connect → Session pooler** (funciona con IPv4,
que es lo que tienen WSL y GitHub Actions; la conexión directa `db.<ref>` es
solo IPv6). Tiene la forma
`postgresql://postgres.<ref>:<contraseña>@aws-0-<región>.pooler.supabase.com:5432/postgres`.

Todas las tablas tienen RLS activado sin políticas: la API pública de Supabase
no expone nada; el job entra como propietario.

### GitHub Actions

`.github/workflows/daily.yml` corre a diario a las 06:30 UTC (y a mano con
*Run workflow*): snapshot, jornadas recién cerradas y cuotas.
`.github/workflows/lineups.yml` captura alineaciones martes y jueves y a diario
de viernes a lunes. Comparten grupo de concurrencia para no pedir a Mister a la
vez. Necesitan estos *repository secrets*: `MISTER_TOKEN`, `MISTER_X_AUTH`,
`MISTER_PHPSESSID`, `MISTER_REFRESH_TOKEN`, `DATABASE_URL`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` y, opcional, `ODDS_API_KEY`. Si la sesión
de Mister caduca o algo falla, llega un aviso por Telegram.

### Renovar la sesión

Cuando `doctor` diga que la sesión ha caducado: iniciar sesión en
mister.mundodeportivo.com, abrir DevTools → Application → Cookies (`token`,
`PHPSESSID`, `refresh-token`) y Network → cualquier petición `/ajax/*`
(cabecera `x-auth`), y copiar los valores a `.env`.

## Desarrollo

```bash
uv run pytest          # tests deterministas, sin red (Postgres embebido vía pgserver)
uv run ruff check src tests
uv run mypy src tests
```

Convenciones: PLAN.md §7. Secretos solo en `.env` / GitHub Secrets.
