# Asistente de Mister Fantasy — Plan de desarrollo

Plan por fases para construir, con Claude Code, un asistente personal que gestione
un equipo de Mister Fantasy (Mundo Deportivo): predice puntos por jornada, propone
el once y genera informes de mercado y cláusulas.

**Cómo usar este documento:** deja este archivo en la raíz del repo y pide a Claude
Code una fase cada vez («implementa la Fase 0»). Cada fase tiene entregables y
criterios de aceptación; no pases a la siguiente sin cumplirlos. Las casillas
`[ ]` son tareas; las marcadas **(Jon)** son manuales.

---

## 1. Contexto y alcance

- Liga privada de LaLiga entre amigos, sistema de puntuación **Mixto**
  (AS + Marca + Mundo Deportivo + SofaScore).
- Objetivo: maximizar puntos a final de temporada. Desempate oficial: valor de
  equipo al inicio de la última jornada, así que el valor es objetivo secundario.
- **Alcance por defecto: solo lectura + recomendaciones.** El sistema lee datos y
  avisa por Telegram; Jon ejecuta en la app. Ejecutar acciones (alinear, pujar,
  clausular) queda como Fase 7 opcional, siempre con aprobación explícita.
- Fuera de alcance: multicuenta, otras ligas, interfaz web (Telegram basta).

### Principios de diseño

1. **Cada pieza en lo suyo.** Modelo estadístico → números. Optimizador →
   decisiones. LLM → leer texto no estructurado, explicar y conversar. El LLM
   nunca inventa una predicción numérica: la consulta.
2. **Guardar todo, crudo, desde el primer día.** El histórico de la liga (pujas,
   cláusulas, saldos rivales) no se puede reconstruir después.
3. **Cada fuente puede fallar sola.** Un scraper roto degrada el informe, no lo
   tumba.
4. **Toda predicción se guarda antes del partido** para poder evaluarla después.
5. **Buen ciudadano.** Pocas peticiones, espaciadas, solo a la cuenta propia.

---

## 2. Reglas del juego verificadas

Fuente: centro de ayuda oficial de Mister (enlaces en §9). Son la especificación
del motor de puntuación y de las restricciones del optimizador.

### 2.1 Puntuación Mixta

Puntos del jugador = **media de las cuatro fuentes**. Cada fuente se convierte así:

| AS (picas) / Marca y MD (estrellas) | Puntos |
|---|---|
| 4 | 14 |
| 3 | 10 |
| 2 | 6 |
| 1 | 2 |
| 0 | −2 |
| S.C. o vacío | 0 |

| Nota SofaScore | Puntos | Nota SofaScore | Puntos |
|---|---|---|---|
| 9.3 – 10 | 12 | 6.8 – 6.9 | 4 |
| 8.6 – 9.2 | 11 | 6.6 – 6.7 | 3 |
| 8.0 – 8.5 | 10 | 6.4 – 6.5 | 2 |
| 7.8 – 7.9 | 9 | 6.2 – 6.3 | 1 |
| 7.6 – 7.7 | 8 | 6.0 – 6.1 | 0 |
| 7.4 – 7.5 | 7 | 5.8 – 5.9 | −1 |
| 7.2 – 7.3 | 6 | 5.4 – 5.7 | −2 |
| 7.0 – 7.1 | 5 | 5.0 – 5.3 | −3 |
| | | 0 – 4.9 | −4 |

Extras, iguales en las cuatro fuentes:

| Evento | Puntos |
|---|---|
| Gol de portero / defensa / centrocampista / delantero | 6 / 5 / 4 / 3 |
| Gol de penalti (cualquier posición) | 3 |
| Gol en propia puerta | 0 (nadie puntúa por ese gol) |
| Roja por doble amarilla | −3 |
| Roja directa | −6 |

**S.C. cuenta como 0 en la media** (no se excluye). Ejemplo oficial: AS 1 pica (2),
Marca S.C., MD S.C., SofaScore 6.5 (2) → (2+0+0+2)/4 = 1 punto.

**Por verificar con datos reales (Fase 1):** redondeo de la media, y si el bonus de
gol/tarjeta se aplica también a una fuente que quedó S.C.

### 2.2 Qué implica para el modelo

- **Tres cuartos de la nota base son cronistas.** Una pica o estrella más en un
  diario vale +4 en esa fuente, es decir **+1 punto mixto**. En SofaScore hacen
  falta ~0.2 de nota para +1 en la fuente, o sea +0.25 mixto. El objetivo
  principal a predecir son las picas/estrellas (ordinal 0–4), no el rating.
- **Los minutos mandan.** Quien juega poco suele quedar S.C. en los diarios, y eso
  entra como 0 en la media (en el ejemplo oficial, 5 minutos = 1 punto).
  El modelo de titularidad y minutos esperados es la pieza de más valor.
- **El gol pesa mucho y más atrás.** Un gol de defensa (+5) equivale a más de una
  estrella en los tres diarios a la vez.
- Los cronistas reaccionan al resultado y al relato del partido: victoria del
  equipo, goles, asistencias y «jugador mediático» son predictores naturales.

### 2.3 Jornada

- Solo puntúan los alineados. El once se fija **antes del primer partido de la
  jornada**; los cambios durante la jornada son de pago con créditos (dinero
  real) → se diseña como si no existieran.
- **Saldo negativo al empezar la jornada = 0 puntos esa jornada.** Restricción
  dura de cualquier recomendación de mercado.
- **−4 puntos por cada hueco vacío** en el once. Siempre hay que alinear 11.
- Bonificación en dinero por punto conseguido (mínimo 10.000 por punto, según
  configuración de la liga; en esta liga 100.000 € por punto más un premio por
  puesto en la jornada, ver `docs/league-rules.md`): los puntos también son caja.

### 2.4 Mercado y cláusulas

- Pujas secretas; gana la más alta y, en empate, la primera. Los fichajes se
  ejecutan en el siguiente ciclo de mercado (frecuencia configurable por liga).
- Al poner un jugador en venta, el juego ofrece ±5% de su valor de mercado:
  la liquidez está prácticamente garantizada a valor de mercado.
- Cláusula por defecto = precio de compra + 50%; si el valor de mercado supera al
  de compra, valor de mercado + 50%. Mínimo 1M si el valor ≤ 666.666.
- Subir cláusula hasta +200%. Coste = 20% de la cláusula máxima ÷ 3 × nº de
  escalón (1 = +100%, 2 = +150%, 3 = +200%). Bajarla devuelve el 50% y bloquea
  volver a subirla 48 h.
- El clausulazo es inmediato y no requiere aprobación.
- Cesiones entre usuarios de 1 a 5 semanas (desactivadas en esta liga).
- Reglas propias de la liga (blindajes, plazo tras fichar, límite de plantilla,
  deuda máxima): leídas de la configuración de Mister en la Fase 0, en
  `docs/league-rules.md`. **Si hay discrepancia, manda la configuración de Mister.**

### 2.5 Términos de uso

Los términos publicados no mencionan bots ni automatización, pero permiten
suspender una cuenta por cualquier motivo y hacen al usuario responsable de su
contraseña. Conclusión operativa: cuenta propia, solo lectura, baja frecuencia,
y nunca guardar la contraseña (solo el token de sesión, como secreto).

---

## 3. Arquitectura

```
Fuentes ──► Ingesta ──► Almacén (Postgres) ──► Modelos ──► Motor de decisión
                              ▲                                   │
                              │                                   ▼
                        Evaluación ◄── predicciones ◄── Agente (Claude) ──► Telegram
```

| Capa | Responsabilidad | Tecnología |
|---|---|---|
| Ingesta | Clientes por fuente, respuesta cruda + normalizada | Python, httpx, Pydantic v2 |
| Almacén | Snapshots append-only, tablas limpias | Supabase (Postgres), migraciones SQL |
| Resolución de identidades | Mismo jugador en todas las fuentes | rapidfuzz + tabla de revisión manual |
| Modelos | Titularidad/minutos, puntos esperados, valor | pandas, scikit-learn, LightGBM |
| Decisión | Once óptimo, pujas, cláusulas, ventas | PuLP (programación entera) |
| Agente | Informe, triaje de noticias, preguntas | SDK de Anthropic con herramientas de solo lectura |
| Entrega | Informes, alertas, conversación | Bot de Telegram |
| Orquestación | Tareas programadas | GitHub Actions (repo **privado**) |
| Calidad | Tests deterministas + auditoría del modelo | pytest |

### Fuentes de datos

| Fuente | Qué aporta | Riesgo |
|---|---|---|
| Mister (`/ajax/*` con sesión propia) | Plantilla, mercado, cláusulas, saldos, plantillas rivales, puntos por jornada, calendario | Sin API oficial; el token caduca |
| Fútbol Fantasy | Probabilidad de titularidad, lesionados, sancionados | HTML frágil, limita peticiones |
| Jornada Perfecta | Respaldo de alineaciones; sección de cronistas | HTML frágil |
| Analítica Fantasy | Histórico de puntos y valor en Mister | HTML frágil |
| Understat | xG, xA, tiros, minutos por partido | Estable |
| API de cuotas (plan gratuito) | 1X2, goles esperados por equipo, goleador | Cuota mensual |
| SofaScore | Nota por partido | Protegido contra scraping → **no depender**; usar la nota que ya da Mister |

---

## 4. Estructura del repo

```
mister-assistant/
├── PLAN.md
├── README.md
├── pyproject.toml
├── .env.example                # nombres de variables, sin valores
├── docs/
│   ├── mister-endpoints.md     # catálogo de endpoints descubiertos (Fase 0)
│   └── league-rules.md         # configuración real de la liga (Fase 0)
├── supabase/migrations/        # SQL versionado
├── src/mister_assistant/
│   ├── config.py               # settings (pydantic-settings)
│   ├── sources/
│   │   ├── mister.py           # cliente de solo lectura
│   │   ├── futbolfantasy.py
│   │   ├── understat.py
│   │   └── odds.py
│   ├── store/                  # repositorios y carga de snapshots
│   ├── identity/               # emparejado de jugadores entre fuentes
│   ├── scoring/                # motor de puntuación Mixta (función pura)
│   ├── models/                 # minutes.py, points.py, value.py
│   ├── decide/                 # lineup.py, market.py, clauses.py
│   ├── agent/                  # herramientas y prompts
│   ├── delivery/telegram.py
│   ├── evals/                  # backtesting y auditoría
│   └── jobs/                   # puntos de entrada de las tareas programadas
├── tests/
└── .github/workflows/
```

---

## 5. Modelo de datos

Todas las tablas de snapshot son **append-only** con `captured_at`. Nunca se
actualiza una fila histórica.

| Tabla | Contenido | Clave |
|---|---|---|
| `raw_responses` | Respuesta cruda de cada petición (jsonb/texto), fuente, fecha | id |
| `players` | Jugador canónico: nombre, equipo, posición en Mister | `mister_player_id` |
| `player_xref` | Id del jugador en cada fuente, confianza, `verified` | (player, source) |
| `fixtures` | Jornada, local, visitante, hora de inicio | id |
| `managers` | Participantes de la liga | `mister_manager_id` |
| `manager_snapshot` | Saldo, valor de equipo, puntos, posición | (manager, captured_at) |
| `squad_snapshot` | Dueño, cláusula, precio de compra, alineado o no | (player, captured_at) |
| `market_snapshot` | Valor de mercado, variación diaria, si está en venta | (player, captured_at) |
| `league_events` | Feed de la liga: fichajes, pujas ganadas, clausulazos, precio | id |
| `player_gameweek` | Por jornada: picas AS, estrellas Marca y MD, nota SofaScore, goles, penaltis, tarjetas, minutos, puntos Mister | (player, jornada) |
| `lineup_forecast` | Probabilidad de titularidad por fuente y momento | (player, jornada, source, captured_at) |
| `match_stats` | xG, xA, tiros, minutos por partido | (player, fixture) |
| `odds` | Probabilidades implícitas por partido y mercado | (fixture, market, captured_at) |
| `predictions` | P(jugar), minutos, puntos esperados, suelo, techo, versión del modelo | (run, player, jornada) |
| `recommendations` | Recomendación emitida y qué pasó después | id |

---

## 6. Fases

### Fase 0 — Descubrimiento y cimientos

Objetivo: hablar con Mister en solo lectura y dejar documentado qué devuelve.

- [x] **(Jon)** Crear repo privado y proyecto de Supabase.
- [x] **(Jon)** Con sesión iniciada en `mister.mundodeportivo.com`, abrir
      DevTools → Network → Fetch/XHR y navegar por feed, mercado, plantilla,
      tabla, ficha de un jugador y una jornada cerrada con desglose de puntos.
      Exportar el HAR a `docs/` (está en `.gitignore`) y copiar los valores de
      sesión (`token`, `x-auth`, `PHPSESSID`, `refresh-token`) a `.env` local.
      **No pegarlos nunca en un chat ni subirlos al repo.**
- [x] **(Jon)** Anotar en `docs/league-rules.md` la configuración de la liga:
      ciclos de mercado, reglas de cláusulas y blindajes, límite de plantilla,
      deuda permitida, bonificación por punto, capitán, cambios en jornada.
- [x] Esqueleto del proyecto: `pyproject.toml`, settings, logging, pytest, CI.
- [x] `sources/mister.py`: cliente httpx de **solo lectura** (lista blanca de
      rutas GET/POST de consulta; cualquier otra ruta lanza excepción), con
      espera entre peticiones y detección de sesión caducada.
- [x] A partir del HAR, escribir `docs/mister-endpoints.md`: ruta, parámetros,
      forma de la respuesta y qué tabla alimenta.
- [x] Investigar si el `refresh-token` permite renovar la sesión sin intervención.
- [x] Comando `mister-assistant doctor` que valida credenciales y lista ligas.

**Aceptación:** `doctor` devuelve la liga de Jon; el catálogo cubre plantilla
propia, plantillas rivales, mercado, saldos, feed, calendario y **desglose de
puntos por fuente y jornada**. Si Mister no expone ese desglose, registrar la
alternativa (sección de cronistas de Jornada Perfecta) antes de seguir.

### Fase 1 — Almacén, snapshot diario y motor de puntuación

Objetivo: empezar a acumular historia y replicar la puntuación al punto.

- [ ] Migraciones de `raw_responses`, `players`, `managers`, `fixtures`, y las
      tablas `*_snapshot`, `league_events`, `player_gameweek`.
- [ ] Job `snapshot_daily`: guarda crudo y normaliza. Idempotente por día.
- [ ] Job `backfill_gameweeks`: carga las jornadas ya disputadas de la temporada.
- [ ] `scoring/mixed.py`: función pura `puntos(as, marca, md, sofa, posición,
      goles, penaltis, tarjetas)` según §2.1.
- [ ] Test de calibración: recalcular todas las filas de `player_gameweek` y
      comparar con los puntos que da Mister. Resolver aquí el redondeo y el
      tratamiento del bonus con S.C.
- [ ] Workflow de GitHub Actions diario; aviso por Telegram si falla o si la
      sesión ha caducado.

**Aceptación:** siete días seguidos de snapshots sin huecos; el motor reproduce
≥ 99% de las puntuaciones históricas y las discrepancias están explicadas.

### Fase 2 — Fuentes externas e identidades

- [ ] Clientes de Fútbol Fantasy (titularidad, bajas), Understat (xG/xA/minutos)
      y cuotas. Cada uno con caché, espera entre peticiones y fallo aislado.
- [ ] `identity/`: emparejado por nombre normalizado + equipo + posición con
      rapidfuzz; lo dudoso va a una cola de revisión (CSV o comando interactivo).
- [ ] Job que captura titularidades varias veces por semana (la serie temporal de
      la probabilidad es un dato en sí).
- [ ] Histórico de temporadas anteriores (puntos Mister y picas/estrellas por
      jornada) si alguna fuente lo ofrece, para entrenar antes.

**Aceptación:** ≥ 98% de los jugadores con minutos esta temporada emparejados en
todas las fuentes; ninguna fuente caída detiene el snapshot diario.

### Fase 3 — Puntos esperados v0, once óptimo e informe de jornada

- [ ] `models/minutes.py`: P(titular), P(suplente que entra), minutos esperados.
      Parte de la probabilidad externa y la corrige con el histórico propio.
- [ ] `models/points.py` v0, sin aprendizaje automático:
      - por jugador, media reciente de cada componente (AS, Marca, MD, SofaScore)
        cuando es titular, con regresión a la media de su posición y equipo;
      - ajuste por rival y localía a partir de las cuotas;
      - bonus de gol = P(gol) × puntos de su posición, con xG por 90 y minutos;
      - escenarios titular / suplente / no juega, con S.C. = 0 en suplencias cortas;
      - salida: media, suelo (p20) y techo (p80).
- [ ] `decide/lineup.py`: programación entera sobre las formaciones que permita
      la liga; nunca deja huecos; marca los puestos frágiles (jugador con baja
      probabilidad de jugar) y propone alternativa.
- [ ] Guardar cada ejecución en `predictions` antes del primer partido.
- [ ] Informe de jornada por Telegram: once recomendado, dudas, cambios respecto
      al once actual y por qué. Se envía la víspera y 3 h antes del primer partido.

**Aceptación:** el informe llega solo antes de cada jornada durante dos jornadas
seguidas y el once propuesto es siempre válido en la app.

### Fase 4 — Mercado y cláusulas

- [ ] `models/value.py`: variación esperada del valor a 7 y 14 días, aprendida de
      `market_snapshot` (tendencia, puntos recientes, titularidad, calendario).
- [ ] Valoración de un jugador = puntos esperados en las próximas N jornadas
      × valor de un punto (incluida la bonificación) + revalorización esperada
      − precio. Comparada siempre contra el titular al que sustituiría.
- [ ] Puja recomendada: distribución de pujas ganadoras de la liga (de
      `league_events`) por rango de valor; tres niveles (ajustada, probable, segura).
- [ ] Cláusulas ofensivas: jugadores rivales cuya cláusula es menor que su
      valoración y cabe en caja, respetando saldo ≥ 0 al inicio de jornada.
- [ ] Cláusulas defensivas: jugadores propios expuestos = cláusula asequible para
      algún rival según su saldo. Decidir subida con la fórmula de coste de §2.4.
- [ ] Ventas: jugadores con caída de valor esperada y bajo aporte de puntos.
- [ ] Informe diario matinal: mercado del día ordenado por valoración, alertas de
      cláusulas, movimientos de rivales desde ayer.

**Aceptación:** informe diario durante dos semanas; toda recomendación queda en
`recommendations` con los números que la justifican.

### Fase 5 — Evaluación y modelo entrenado

- [ ] `evals/`: por jornada cerrada, error medio y sesgo de los puntos esperados,
      correlación de rangos, calibración de P(jugar) por tramos, y comparación de
      once recomendado vs once alineado vs once «los 11 más caros».
- [ ] Auditoría de mercado: evolución de valor y puntos de lo recomendado comprar
      frente a lo recomendado evitar.
- [ ] `models/points.py` v1: modelo ordinal para picas/estrellas por diario y
      regresión para la nota SofaScore (LightGBM), con validación temporal.
      Solo sustituye a v0 si lo mejora en el backtest.
- [ ] Resumen de evaluación semanal por Telegram.

**Aceptación:** existe un informe reproducible que dice si v1 mejora a v0 y si el
once recomendado bate al alineado.

### Fase 6 — Agente conversacional

- [ ] Herramientas de solo lectura para Claude: consultar jugador, comparar dos
      jugadores, simular once, valorar un fichaje, estado de rivales.
- [ ] Triaje de noticias: resumir ruedas de prensa y partes médicos y convertirlos
      en ajustes estructurados de disponibilidad, con fuente y fecha.
- [ ] Conversación por Telegram restringida al chat de Jon.
- [ ] Casos de prueba del agente: preguntas tipo con respuesta esperada; el agente
      debe citar siempre cifras salidas de las herramientas.

**Aceptación:** responde a «¿vendo a X?», «¿pujo por Y y cuánto?» y «¿a quién
siento esta jornada?» con cifras trazables a `predictions`.

### Fase 7 (opcional) — Acciones con aprobación

Solo si Jon lo decide tras leer §2.5. Cliente de escritura separado, desactivado
por defecto, que ejecuta **una acción por aprobación explícita** en Telegram
(botón), con registro de auditoría y límite diario. Nada se ejecuta solo.

---

## 7. Convenciones para Claude Code

- Python 3.12, `uv`, tipado estricto, Pydantic v2 en todas las fronteras.
- Tests deterministas con respuestas grabadas; ningún test llama a la red.
- Secretos solo en `.env` local y en GitHub Secrets. Nunca en código, logs ni tests.
- El cliente de Mister es de solo lectura hasta la Fase 7; no añadir rutas de
  escritura «por si acaso».
- Los parsers de HTML viven aislados por fuente y fallan con error tipado.
- Cada fase termina con README actualizado y un comando para ejecutarla en local.

## 8. Decisiones abiertas

| Decisión | Por defecto | Cuándo revisarla |
|---|---|---|
| ¿Recomendar o ejecutar? | Solo recomendar | Tras la Fase 6 |
| ¿Dónde corre? | GitHub Actions | Si la sesión de Mister no se puede renovar sola → máquina propia |
| Horizonte de valoración | 5 jornadas | Con datos de la Fase 5 |
| Fuente de picas/estrellas históricas | Mister | Si Mister no las expone (Fase 0) |

## 9. Referencias

- Reglas del juego: https://help.playmister.com/article/142-reglas-del-juego
- Puntuación Mixta: https://help.playmister.com/article/161-sistema-de-puntuacion-mixto-mixta
- Caso S.C. en Mixta: https://help.playmister.com/article/166-no-me-cuadra-la-puntuacion-mixta-de-un-jugador
- Tablas de puntos: [SofaScore](https://help.playmister.com/article/96-puntos-sofascore) ·
  [AS](https://help.playmister.com/article/63-points-as) ·
  [Marca](https://help.playmister.com/article/64-points-marca) ·
  [Mundo Deportivo](https://help.playmister.com/article/100-puntos-fantasy-diario-md)
- Cambios durante la jornada: https://help.playmister.com/article/40-se-pueden-realizar-cambios-durante-la-jornada-de-liga
- Términos y condiciones: https://help.playmister.com/article/77-terminos-y-condiciones-de-mister
- Proyectos de referencia: https://github.com/elexrallen/Mister ·
  https://github.com/javica98/MisterFantasy_analytics
