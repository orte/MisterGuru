# Mister Assistant

Asistente personal (solo lectura) para un equipo de Mister Fantasy. El plan
completo está en [PLAN.md](PLAN.md).

## Estado

**Fase 0 — descubrimiento y cimientos.**

- Cliente de Mister de solo lectura con lista blanca de rutas
  (`src/mister_assistant/sources/mister.py`).
- Catálogo de endpoints: [docs/mister-endpoints.md](docs/mister-endpoints.md).
- Reglas de la liga: [docs/league-rules.md](docs/league-rules.md).
- Comando `doctor`.

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

### Renovar la sesión

Cuando `doctor` diga que la sesión ha caducado: iniciar sesión en
mister.mundodeportivo.com, abrir DevTools → Application → Cookies (`token`,
`PHPSESSID`, `refresh-token`) y Network → cualquier petición `/ajax/*`
(cabecera `x-auth`), y copiar los valores a `.env`.

## Desarrollo

```bash
uv run pytest          # tests deterministas, sin red
uv run ruff check src tests
uv run mypy src tests
```

Convenciones: PLAN.md §7. Secretos solo en `.env` / GitHub Secrets.
